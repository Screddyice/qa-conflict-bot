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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from .. import git_ops, llm
from ..config import Config, LLMConfig, QAConfig, load_repo_override
from ..server import PRJob
from . import methodology, report
from .browse import BrowseEngine, SubprocessBrowse
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
LoadQAFn = Callable[[Path], QAConfig]
CleanupFn = Callable[[Path], Awaitable[None]]


@dataclass(frozen=True)
class QADeps:
    load_qa: LoadQAFn
    clone: CloneFn
    open_url: OpenURLFn
    browse: BrowseEngine
    complete: CompleteFn
    cleanup: CleanupFn


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


def default_deps(cfg: Config, browse_binary: str) -> QADeps:
    return QADeps(
        load_qa=lambda root: load_repo_override(root).qa,
        clone=_default_clone,
        open_url=serve_via_start_command,
        browse=SubprocessBrowse(browse_binary),
        complete=llm.complete,
        cleanup=git_ops.cleanup,
    )


async def process_qa_job(job: PRJob, cfg: Config, gh: _GH, deps: QADeps) -> None:
    L = log.bind(delivery=job.delivery_id, owner=job.owner, repo=job.repo, pr=job.pr_number, flow="qa")
    repo_dir: Path | None = None
    failure: str | None = None
    url: str | None = None
    findings: list[report.Finding] = []
    score = 10.0

    try:
        # We clone before the enabled check because [qa] lives in the repo's
        # .pr-conflict-bot.toml — same clone-then-check pattern as the conflict flow.
        repo_dir = await deps.clone(job, cfg, gh)
        qa = deps.load_qa(repo_dir)
        if not qa.enabled:
            L.info("qa disabled by repo config")
            return

        try:
            async with deps.open_url(repo_dir, qa) as live_url:
                url = live_url
                screenshot = repo_dir / ".qa-screenshot.png"
                state = await deps.browse.capture(url, screenshot_to=screenshot)
                raw = await deps.complete(methodology.build_smoke_prompt(state), cfg.llm)
                findings = methodology.parse_findings(raw)
                score = report.health_score(findings)
        except URLResolutionError as e:
            failure = f"could not get a live URL to test: {e}"
            L.warning("qa url resolution failed", reason=str(e))

        comment = report.format_comment(url=url or "", findings=findings, score=score, failure=failure)
        await gh.post_issue_comment(job.installation_id, job.owner, job.repo, job.pr_number, comment)
        L.info("qa done", findings=len(findings), score=score, failure=failure)
    except Exception as e:  # report, never crash the worker
        L.exception("qa job failed")
        with contextlib.suppress(Exception):
            await gh.post_issue_comment(
                job.installation_id, job.owner, job.repo, job.pr_number,
                report.format_comment(url="", findings=[], score=0.0, failure=f"unexpected error: {e}"),
            )
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
