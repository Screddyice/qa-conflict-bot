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

from .. import git_ops, llm
from ..config import Config, LLMConfig, QAConfig, RepoOverride, load_repo_override
from ..linear import LinearClient
from ..qa_policy import resolve_qa
from ..server import PRJob
from . import methodology, report
from .browse import BrowseEngine, SubprocessBrowse
from .detect import detect_serve
from .url_resolver import URLResolutionError, serve_via_start_command

log = structlog.get_logger()


class _GH(Protocol):
    async def clone_url(self, installation_id: int, owner: str, repo: str) -> str: ...
    async def post_issue_comment(
        self, installation_id: int, owner: str, repo: str, pr_number: int, body: str
    ) -> None: ...


CompleteFn = Callable[[str, LLMConfig], Awaitable[str]]
OpenURLFn = Callable[[Path, QAConfig], AbstractAsyncContextManager[str]]
CloneFn = Callable[[PRJob, Config, _GH], Awaitable[Path]]
LoadQAFn = Callable[[Path], RepoOverride]
CleanupFn = Callable[[Path], Awaitable[None]]
# (owner, pr_url, body) -> True if a Linear comment was posted, False if skipped
# (no token for the owner, or no Linear issue linked to the PR).
NotifyLinearFn = Callable[[str, str, str], Awaitable[bool]]


async def _noop_notify_linear(owner: str, pr_url: str, body: str) -> bool:
    return False


@dataclass(frozen=True)
class QADeps:
    load_qa: LoadQAFn
    clone: CloneFn
    open_url: OpenURLFn
    browse: BrowseEngine
    complete: CompleteFn
    cleanup: CleanupFn
    notify_linear: NotifyLinearFn = _noop_notify_linear


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
