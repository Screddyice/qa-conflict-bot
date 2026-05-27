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
from ..config import Config, LLMConfig, QAConfig, RepoOverride, VerifyConfig, load_repo_override
from ..linear import LinearClient
from ..qa_policy import resolve_qa
from ..server import PRJob
from ..verify import VerifyResult
from . import methodology, report
from .browse import BrowseEngine, PageState, SubprocessBrowse
from .detect import detect_serve
from .report import Finding
from .url_resolver import URLResolutionError, serve_via_start_command

log = structlog.get_logger()


# Idempotency marker. Every QA comment carries a hidden HTML comment naming the
# head SHA it reviewed. Before re-running, we skip a SHA we've already commented
# on — without this, a strict reviewer re-judges and re-suggests fixes on every
# redelivered/duplicate webhook (and on `opened`+`synchronize` for one commit),
# which is the "keeps suggesting new fixes" loop.
_REVIEW_MARKER_PREFIX = "qa-conflict-bot:reviewed="


def _review_marker(sha: str) -> str:
    return f"\n\n<!-- {_REVIEW_MARKER_PREFIX}{sha} -->"


def _already_reviewed(comment_bodies: list[str], sha: str) -> bool:
    needle = f"{_REVIEW_MARKER_PREFIX}{sha}"
    return any(needle in body for body in comment_bodies)


class _GH(Protocol):
    async def clone_url(self, installation_id: int, owner: str, repo: str) -> str: ...
    async def post_issue_comment(
        self, installation_id: int, owner: str, repo: str, pr_number: int, body: str
    ) -> None: ...
    async def list_self_comment_bodies(
        self, installation_id: int, owner: str, repo: str, pr_number: int
    ) -> list[str]: ...


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
    `verified` = the verify gate passed (only meaningful if changed); `pushed` =
    the fix commit was pushed onto the PR's own branch (only when changed and
    verified); `detail` = a short note (e.g. the verify summary, or why the push
    didn't happen) for the explanatory PR comment."""
    changed: bool
    verified: bool
    pushed: bool
    detail: str


# (job, cfg, gh, repo_dir, fix_prompt, findings) -> FixOutcome. fix_prompt is the
# editing instruction (page-based or diff-based); findings drive the comment.
RunFixFn = Callable[
    ["PRJob", "Config", "_GH", Path, str, list[Finding]], Awaitable[FixOutcome]
]
PrDiffFn = Callable[[Path, str], Awaitable[str]]
RunVerifyFn = Callable[[VerifyConfig, Path], Awaitable[VerifyResult]]


async def _noop_notify_linear(owner: str, pr_url: str, body: str) -> bool:
    return False


async def _noop_run_fix(
    job: PRJob, cfg: Config, gh: _GH, repo_dir: Path, fix_prompt: str, findings: list[Finding]
) -> FixOutcome:
    return FixOutcome(changed=False, verified=False, pushed=False, detail="")


async def _default_pr_diff(repo_dir: Path, base_branch: str) -> str:
    return await git_ops.pr_diff(repo_dir, base_branch)


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
    pr_diff: PrDiffFn = _default_pr_diff
    run_verify: RunVerifyFn = verify.run


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


def _has_verify_gate(v: VerifyConfig) -> bool:
    """True if the verify config has at least one non-empty step. Fix mode refuses
    to push without one — an empty gate trivially 'passes' (every step skips),
    which would mean pushing unverified AI edits onto the PR. This is the
    load-bearing guard for org-wide fix mode: fixes only land where there's a way
    to check them."""
    return bool(v.lint.strip() or v.typecheck.strip() or v.test.strip())


async def default_run_fix(
    job: PRJob, cfg: Config, gh: _GH, repo_dir: Path, fix_prompt: str, findings: list[Finding]
) -> FixOutcome:
    """Real fix flow: verify-gate check → LLM edits the clone (already checked out
    on the PR's head branch) → verify gate → commit + push onto the PR's OWN
    branch. One sweep: the fixes land on the PR itself ("resubmit with fixes"),
    not a separate side-PR, so the PR's diff actually improves and re-reviewing it
    is unnecessary. The resulting push is a bot-authored `synchronize` that the
    server's self-trigger guard skips, so it never re-QAs its own fix (no loop).

    Works for both web (page-based prompt) and code (diff-based prompt) QA — the
    caller supplies the editing prompt.

    Guards: no verify gate → no edit, no push; no edits → no push; verify fail →
    no push; push rejected (e.g. fork PR / branch protection / concurrent human
    push) → reported, not silent. RS21 never reaches here (resolve_qa forces
    report mode)."""
    if not _has_verify_gate(cfg.verify):
        # No way to verify the fix → don't push unverified edits. Stay quiet
        # (changed=False) so org-wide auto repos without a gate just report.
        return FixOutcome(changed=False, verified=False, pushed=False, detail="no verify gate configured")

    await llm.apply_edit(fix_prompt, cfg.llm, cwd=repo_dir)
    if not await git_ops.has_changes(repo_dir):
        return FixOutcome(changed=False, verified=False, pushed=False, detail="model made no edits")

    # Capture exactly what the model touched BEFORE the verify gate runs. The gate
    # creates a venv/build artifacts in the clone; we must commit only the model's
    # edits, never those artifacts (a blanket `git add -A` here once pushed the
    # whole verify venv onto a PR).
    edited = await git_ops.changed_paths(repo_dir)

    vr = await verify.run(cfg.verify, repo_dir)
    if not vr.passed:
        return FixOutcome(changed=True, verified=False, pushed=False, detail=vr.summary())

    await git_ops.commit_paths(
        repo_dir, edited, f"fix(qa): address {len(findings)} QA finding(s) on #{job.pr_number}"
    )
    # Push the fix onto the PR's own head branch. force-with-lease guards a
    # concurrent human push: if origin moved off pr_head_sha, the push is
    # rejected and we report it rather than clobbering their work.
    try:
        await git_ops.push_with_lease(repo_dir, job.pr_branch, job.pr_head_sha)
    except git_ops.GitError as e:
        return FixOutcome(
            changed=True, verified=True, pushed=False,
            detail=f"verify passed but the fix could not be pushed to the PR branch: {e}",
        )
    return FixOutcome(changed=True, verified=True, pushed=True, detail="")


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
    # Stamp every QA comment with the reviewed head SHA so re-deliveries of the
    # same commit are skipped (see _already_reviewed in process_qa_job).
    await gh.post_issue_comment(
        job.installation_id, job.owner, job.repo, job.pr_number,
        body + _review_marker(job.pr_head_sha),
    )


async def _post_fix_outcome(gh: _GH, job: PRJob, outcome: FixOutcome, n_findings: int) -> None:
    """Comment on the PR about a fix attempt. Shared by web + code QA."""
    if outcome.pushed:
        await _comment(gh, job,
            f"**pr-conflict-bot: QA** applied fixes for the {n_findings} finding(s) above, "
            "the verify gate passed, and pushed them to this PR. Re-review the updated diff — "
            "nothing auto-merges.")
    elif outcome.changed and outcome.verified and not outcome.pushed:
        await _comment(gh, job,
            "**pr-conflict-bot: QA** fixed the finding(s) and the verify gate passed, but "
            f"could not push to the PR branch.\n\n```\n{outcome.detail}\n```")
    elif outcome.changed and not outcome.verified:
        await _comment(gh, job,
            "**pr-conflict-bot: QA** attempted a fix but the verify gate failed — "
            f"nothing pushed.\n\n```\n{outcome.detail}\n```")


async def _code_qa(
    job: PRJob, cfg: Config, gh: _GH, deps: QADeps, override: RepoOverride,
    qa: QAConfig, repo_dir: Path,
) -> None:
    """Code-level QA for repos with no servable web app: LLM reviews the PR diff +
    the verify gate runs; findings → comment (+ Linear) and a fix pushed to the
    PR in fix mode.

    SILENT on a clean diff — org-wide code review must not comment on every PR.
    """
    L = log.bind(delivery=job.delivery_id, owner=job.owner, repo=job.repo, pr=job.pr_number, flow="qa-code")
    diff = await deps.pr_diff(repo_dir, job.base_branch)
    if not diff.strip():
        L.info("qa code: empty diff, nothing to review")
        return

    raw = await deps.complete(methodology.build_diff_review_prompt(diff), cfg.llm)
    findings = methodology.parse_findings(raw)

    # Run the verify/test gate if the repo has one; a failure is itself a finding.
    eff_verify = cfg.verify if override.verify is None else override.verify
    if _has_verify_gate(eff_verify):
        try:
            vr = await deps.run_verify(eff_verify, repo_dir)
            if not vr.passed:
                findings.append(Finding("high", "verify gate failing", vr.summary()[:600]))
        except Exception:
            L.warning("qa code: verify gate run failed", exc_info=True)

    if not findings:
        L.info("qa code: clean diff, staying quiet")
        return

    score = report.health_score(findings)
    await _comment(gh, job, report.format_code_comment(findings=findings, score=score))

    pr_url = f"https://github.com/{job.owner}/{job.repo}/pull/{job.pr_number}"
    try:
        await deps.notify_linear(
            job.owner, pr_url,
            report.format_linear_comment(pr_url=pr_url, url="(code review)", findings=findings, score=score),
        )
    except Exception:
        L.warning("qa code linear notify failed", exc_info=True)

    if qa.mode == "fix":
        fix_cfg = cfg if override.verify is None else replace(cfg, verify=override.verify)
        try:
            outcome = await deps.run_fix(
                job, fix_cfg, gh, repo_dir, methodology.build_code_fix_prompt(diff, findings), findings
            )
            await _post_fix_outcome(gh, job, outcome, len(findings))
            L.info("qa code fix done", changed=outcome.changed,
                   verified=outcome.verified, pushed=outcome.pushed)
        except Exception:
            L.warning("qa code fix flow failed", exc_info=True)


async def process_qa_job(job: PRJob, cfg: Config, gh: _GH, deps: QADeps) -> None:
    L = log.bind(delivery=job.delivery_id, owner=job.owner, repo=job.repo, pr=job.pr_number, flow="qa")
    repo_dir: Path | None = None
    # The screenshot is written OUTSIDE the clone: in fix mode we push the LLM's
    # edits onto the PR with `git add -A`, and a screenshot inside the tree would
    # get committed onto the PR. work_dir is the browse sandbox root, so it's a
    # legal target.
    screenshot: Path | None = None
    # "explicit" = the repo asked for QA itself (set [qa] enabled, or gave a start
    # command). Explicit repos get failure comments; org-default ("auto") repos
    # skip silently on failure so we don't spam every PR across the org.
    explicit = False

    try:
        # Idempotency: if we already posted a QA result for this exact head SHA,
        # don't re-review it. Best-effort — a listing failure must never block QA
        # (fall through and run, same as before). Done before the clone so a
        # redelivered webhook costs one cheap API call, not a full QA pass.
        try:
            prior = await gh.list_self_comment_bodies(
                job.installation_id, job.owner, job.repo, job.pr_number
            )
            if _already_reviewed(prior, job.pr_head_sha):
                L.info("qa: head sha already reviewed, skipping", sha=job.pr_head_sha[:8])
                return
        except Exception:
            L.warning("qa: could not check prior comments; proceeding", exc_info=True)

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
        # No servable web app → fall back to code-level QA (review the diff).
        if not qa.start:
            spec = detect_serve(repo_dir)
            if spec is None:
                await _code_qa(job, cfg, gh, deps, override, qa, repo_dir)
                return
            qa = replace(qa, start=spec.start, url=spec.url, build=spec.build)

        url: str | None = None
        state: PageState | None = None
        findings: list[report.Finding] = []
        score = 10.0
        try:
            async with deps.open_url(repo_dir, qa) as live_url:
                url = live_url
                screenshot = cfg.work_dir / f".qa-screenshot-{job.delivery_id}.png"
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

        # Fix mode: on findings, let the backend fix them, verify, and push the
        # fix onto this PR. mode is "report" for RS21 (resolve_qa forces it) so
        # this is skipped there. Best-effort: a fix failure never breaks the
        # report path above.
        if qa.mode == "fix" and findings and state is not None:
            # Use the repo's own [verify] gate when it set one, else env defaults —
            # fix mode requires a real gate (default_run_fix enforces this).
            fix_cfg = cfg if override.verify is None else replace(cfg, verify=override.verify)
            try:
                outcome = await deps.run_fix(
                    job, fix_cfg, gh, repo_dir,
                    methodology.build_fix_prompt(state, findings), findings,
                )
                await _post_fix_outcome(gh, job, outcome, len(findings))
                L.info("qa fix done", changed=outcome.changed,
                       verified=outcome.verified, pushed=outcome.pushed)
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
        if screenshot is not None:
            with contextlib.suppress(Exception):
                screenshot.unlink(missing_ok=True)
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
