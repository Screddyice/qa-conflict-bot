"""QA flow: clone the PR, get a live URL, capture the page, judge it, comment.

Report-only (M1). Dependencies are injected via QADeps so the flow is testable
without a real browser, model, git, or network. Real wiring is in `default_deps()`;
tests pass fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import aiohttp
import structlog

from .. import git_ops, llm, verify
from ..config import Config, LLMConfig, QAConfig, RepoOverride, load_repo_override
from ..linear import LinearClient
from ..qa_policy import resolve_qa
from ..server import PRJob
from . import methodology, report
from .browse import BrowseEngine, PageState, SubprocessBrowse
from .detect import detect_serve
from .report import Finding
from .url_resolver import URLResolutionError, serve_via_start_command

log = structlog.get_logger()


class _GH(Protocol):
    async def clone_url(self, installation_id: int, owner: str, repo: str) -> str: ...
    async def post_issue_comment(
        self, installation_id: int, owner: str, repo: str, pr_number: int, body: str
    ) -> None: ...
    async def create_pull_request(
        self, installation_id: int, owner: str, repo: str,
        *, head: str, base: str, title: str, body: str,
    ) -> str: ...


CompleteFn = Callable[[str, LLMConfig], Awaitable[str]]
OpenURLFn = Callable[[Path, QAConfig], AbstractAsyncContextManager[str]]
CloneFn = Callable[[PRJob, Config, _GH], Awaitable[Path]]
LoadQAFn = Callable[[Path], RepoOverride]
CleanupFn = Callable[[Path], Awaitable[None]]
# (owner, pr_url, body) -> True if a Linear comment was posted, False if skipped
# (no token for the owner, or no Linear issue linked to the PR).
NotifyLinearFn = Callable[[str, str, str], Awaitable[bool]]


@dataclass(frozen=True)
class FixOutcome:
    """Result of a fix-mode attempt. `changed` = the LLM edited something;
    `verified` = the verify gate passed (only meaningful if changed); `pr_url` =
    the opened fix PR (only when changed and verified); `detail` = a short note
    (e.g. the verify summary) for the explanatory PR comment."""
    changed: bool
    verified: bool
    pr_url: str | None
    detail: str


# (job, cfg, gh, repo_dir, page_state, findings) -> FixOutcome
RunFixFn = Callable[
    ["PRJob", "Config", "_GH", Path, PageState, list[Finding]], Awaitable[FixOutcome]
]


async def _noop_notify_linear(owner: str, pr_url: str, body: str) -> bool:
    return False


async def _noop_run_fix(
    job: PRJob, cfg: Config, gh: _GH, repo_dir: Path, state: PageState, findings: list[Finding]
) -> FixOutcome:
    return FixOutcome(changed=False, verified=False, pr_url=None, detail="")


@dataclass(frozen=True)
class QADeps:
    load_qa: LoadQAFn
    clone: CloneFn
    open_url: OpenURLFn
    browse: BrowseEngine
    complete: CompleteFn
    cleanup: CleanupFn
    notify_linear: NotifyLinearFn = _noop_notify_linear
    run_fix: RunFixFn = _noop_run_fix


async def _default_clone(job: PRJob, cfg: Config, gh: _GH) -> Path:
    clone_url = await gh.clone_url(job.installation_id, job.owner, job.repo)
    spec = git_ops.CloneSpec(
        clone_url=clone_url,
        pr_branch=job.pr_branch,
        base_branch=job.base_branch,
        pr_head_sha=job.pr_head_sha,
        work_dir=cfg.work_dir,
    )
    return await git_ops.clone_pr(spec, git_name=cfg.identity.git_name, git_email=cfg.identity.git_email)


def _fix_pr_body(job: PRJob, findings: list[Finding]) -> str:
    lines = [
        f"Automated QA fix for #{job.pr_number}.",
        "",
        "Addresses the findings QA reported on that PR:",
    ]
    for f in findings:
        lines.append(f"- **[{f.severity}]** {f.title} — {f.detail}")
    lines += [
        "",
        "_Opened by QA fix mode. The verify gate passed before this PR was created. "
        "Review before merge — nothing auto-merges._",
    ]
    return "\n".join(lines)


def _has_verify_gate(v: object) -> bool:
    """True if the verify config has at least one non-empty step. Fix mode refuses
    to open a PR without one — an empty gate trivially 'passes' (every step skips),
    which would mean shipping unverified AI edits. This is the load-bearing guard
    for org-wide fix mode: PRs only get opened where there's a way to check them."""
    from ..config import VerifyConfig
    assert isinstance(v, VerifyConfig)
    return bool(v.lint.strip() or v.typecheck.strip() or v.test.strip())


async def default_run_fix(
    job: PRJob, cfg: Config, gh: _GH, repo_dir: Path, state: PageState, findings: list[Finding]
) -> FixOutcome:
    """Real fix flow: verify-gate check → LLM edits the clone → verify gate → new
    branch + fix PR.

    Guards: no verify gate → no PR (don't even edit); no edits → no PR; verify
    fail → no PR; success → a PR (never merged). RS21 never reaches here
    (resolve_qa forces report mode).
    """
    if not _has_verify_gate(cfg.verify):
        # No way to verify the fix → don't open an unverified PR. Stay quiet
        # (changed=False) so org-wide auto repos without a gate just report.
        return FixOutcome(changed=False, verified=False, pr_url=None, detail="no verify gate configured")

    await llm.apply_edit(methodology.build_fix_prompt(state, findings), cfg.llm, cwd=repo_dir)
    if not await git_ops.has_changes(repo_dir):
        return FixOutcome(changed=False, verified=False, pr_url=None, detail="model made no edits")

    vr = await verify.run(cfg.verify, repo_dir)
    if not vr.passed:
        return FixOutcome(changed=True, verified=False, pr_url=None, detail=vr.summary())

    branch = f"qa-fix/{job.pr_branch}-{job.pr_head_sha[:8]}"
    await git_ops.create_branch(repo_dir, branch)
    await git_ops.stage_and_commit_resolution(
        repo_dir, f"fix(qa): address {len(findings)} QA finding(s) on #{job.pr_number}"
    )
    await git_ops.push_new_branch(repo_dir, branch)
    pr_url = await gh.create_pull_request(
        job.installation_id, job.owner, job.repo,
        head=branch, base=job.pr_branch,
        title=f"QA fix for #{job.pr_number}: {len(findings)} finding(s)",
        body=_fix_pr_body(job, findings),
    )
    return FixOutcome(changed=True, verified=True, pr_url=pr_url, detail="")


def default_deps(cfg: Config, browse_binary: str, session: aiohttp.ClientSession) -> QADeps:
    async def _notify_linear(owner: str, pr_url: str, body: str) -> bool:
        token = cfg.linear_tokens.get(owner.lower())
        if not token:
            return False  # this owner has no Linear configured — skip silently
        client = LinearClient(token, session)
        issue_id = await client.find_issue_id_by_url(pr_url)
        if issue_id is None:
            return False  # no Linear issue linked to this PR (yet)
        await client.comment(issue_id, body)
        return True

    return QADeps(
        load_qa=lambda root: load_repo_override(root),
        clone=_default_clone,
        open_url=serve_via_start_command,
        browse=SubprocessBrowse(browse_binary, cwd=cfg.work_dir),
        complete=llm.complete,
        cleanup=git_ops.cleanup,
        notify_linear=_notify_linear,
        run_fix=default_run_fix,
    )


async def _comment(gh: _GH, job: PRJob, body: str) -> None:
    await gh.post_issue_comment(job.installation_id, job.owner, job.repo, job.pr_number, body)


async def process_qa_job(job: PRJob, cfg: Config, gh: _GH, deps: QADeps) -> None:
    L = log.bind(delivery=job.delivery_id, owner=job.owner, repo=job.repo, pr=job.pr_number, flow="qa")
    repo_dir: Path | None = None
    # "explicit" = the repo asked for QA itself (set [qa] enabled, or gave a start
    # command). Explicit repos get failure comments; org-default ("auto") repos
    # skip silently on failure so we don't spam every PR across the org.
    explicit = False

    try:
        # Clone before the enabled check because [qa] lives in the repo's
        # .pr-conflict-bot.toml — same clone-then-check pattern as the conflict flow.
        repo_dir = await deps.clone(job, cfg, gh)
        override = deps.load_qa(repo_dir)
        qa = resolve_qa(override, cfg, job.owner, job.repo)  # org default + RS21 block
        if not qa.enabled:
            L.info("qa disabled (repo opt-out, not in default orgs, or RS21)")
            return
        explicit = override.qa_enabled_set or bool(override.qa.start)

        # Resolve how to serve the app: repo-provided [qa] start, else auto-detect.
        if not qa.start:
            spec = detect_serve(repo_dir)
            if spec is None:
                if explicit:
                    await _comment(gh, job, report.format_comment(
                        url="", findings=[], score=0.0,
                        failure="couldn't detect how to build/serve this app — "
                                "set `[qa] start`/`url` in .pr-conflict-bot.toml",
                    ))
                else:
                    L.info("qa auto: no servable app detected, skipping silently")
                return
            qa = replace(qa, start=spec.start, url=spec.url, build=spec.build)

        url: str | None = None
        state: PageState | None = None
        findings: list[report.Finding] = []
        score = 10.0
        try:
            async with deps.open_url(repo_dir, qa) as live_url:
                url = live_url
                screenshot = repo_dir / ".qa-screenshot.png"
                state = await deps.browse.capture(url, screenshot_to=screenshot)
                raw = await deps.complete(methodology.build_smoke_prompt(state), cfg.llm)
                findings = methodology.parse_findings(raw)
                score = report.health_score(findings)
        except URLResolutionError as e:
            if not explicit:
                L.info("qa auto: app would not serve, skipping silently", reason=str(e))
                return
            await _comment(gh, job, report.format_comment(
                url="", findings=[], score=0.0,
                failure=f"could not get a live URL to test: {e}",
            ))
            return

        # Successful run — always post the report (this is the value for auto repos).
        await _comment(gh, job, report.format_comment(
            url=url or "", findings=findings, score=score, failure=None,
        ))

        # When QA found issues, mirror them to the PR's Linear ticket. Best-effort:
        # a Linear failure must never break the QA flow — the PR comment is truth.
        if findings:
            pr_url = f"https://github.com/{job.owner}/{job.repo}/pull/{job.pr_number}"
            linear_body = report.format_linear_comment(
                pr_url=pr_url, url=url or "", findings=findings, score=score
            )
            try:
                posted = await deps.notify_linear(job.owner, pr_url, linear_body)
                L.info("qa linear notify", posted=posted)
            except Exception:
                L.warning("qa linear notify failed", exc_info=True)

        # Fix mode: on findings, let the backend fix them, verify, and open a fix
        # PR. mode is "report" for RS21 (resolve_qa forces it) so this is skipped
        # there. Best-effort: a fix failure never breaks the report path above.
        if qa.mode == "fix" and findings and state is not None:
            # Use the repo's own [verify] gate when it set one, else env defaults —
            # fix mode requires a real gate (default_run_fix enforces this).
            fix_cfg = cfg if override.verify is None else replace(cfg, verify=override.verify)
            try:
                outcome = await deps.run_fix(job, fix_cfg, gh, repo_dir, state, findings)
                if outcome.pr_url:
                    await _comment(gh, job,
                        f"**pr-conflict-bot: QA** opened a fix PR for the "
                        f"{len(findings)} finding(s) above: {outcome.pr_url}\n\n"
                        "_Verify gate passed. Review before merge — nothing auto-merges._")
                elif outcome.changed and not outcome.verified:
                    await _comment(gh, job,
                        "**pr-conflict-bot: QA** attempted a fix but the verify gate "
                        f"failed — no PR opened.\n\n```\n{outcome.detail}\n```")
                L.info("qa fix done", changed=outcome.changed,
                       verified=outcome.verified, pr=outcome.pr_url)
            except Exception:
                L.warning("qa fix flow failed", exc_info=True)

        L.info("qa done", findings=len(findings), score=score)
    except Exception as e:  # never crash the worker
        L.exception("qa job failed")
        # Only surface unexpected errors to repos that opted in — auto repos stay quiet.
        if explicit:
            with contextlib.suppress(Exception):
                await _comment(gh, job, report.format_comment(
                    url="", findings=[], score=0.0, failure=f"unexpected error: {e}",
                ))
    finally:
        if repo_dir is not None:
            with contextlib.suppress(Exception):
                await deps.cleanup(repo_dir)


async def qa_worker(queue: asyncio.Queue[PRJob], cfg: Config, gh: _GH, deps: QADeps) -> None:
    """Single QA worker. Independent of the conflict worker pool."""
    while True:
        job = await queue.get()
        try:
            await process_qa_job(job, cfg, gh, deps)
        except Exception:
            log.exception("qa worker top-level exception", delivery=job.delivery_id)
        finally:
            queue.task_done()
