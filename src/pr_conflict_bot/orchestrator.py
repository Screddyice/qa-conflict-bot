"""Main flow: dequeue PR jobs, resolve conflicts, push back, comment.

The orchestrator owns git. The LLM (Claude or Codex) is scoped to producing
resolved file content for one file at a time. The verify gate is the safety
net — nothing pushes if it fails.
"""

from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path

import structlog

from . import git_ops, llm, verify
from .config import Config, LLMConfig, RepoOverride, VerifyConfig, load_repo_override
from .github_api import GitHubClient
from .server import PRJob

log = structlog.get_logger()


def _path_skipped(file_path: str, override: RepoOverride) -> bool:
    return any(fnmatch.fnmatch(file_path, pat) for pat in override.skip_paths)


def _effective_verify(default: VerifyConfig, override: RepoOverride) -> VerifyConfig:
    return override.verify or default


def _fmt_summary_comment(
    *,
    base_branch: str,
    conflicted: list[str],
    resolved: list[str],
    skipped: list[str],
    verify_result: verify.VerifyResult | None,
    pushed_sha: str | None,
    failure: str | None,
) -> str:
    lines = ["**pr-conflict-bot**: merge-conflict resolution attempt", ""]
    lines.append(f"Merging `origin/{base_branch}` into PR branch.")
    lines.append("")
    # Failure path takes precedence: if something blew up before we even got to
    # the merge step, `conflicted` will be empty but `failure` will be set.
    # Don't lie and say "no conflicts" in that case.
    if failure and not conflicted:
        lines.append(f"**Did not run.** {failure}")
        return "\n".join(lines)
    if not conflicted:
        lines.append("No conflicts found — branch already merges cleanly.")
        return "\n".join(lines)

    lines.append(f"**Conflicted files ({len(conflicted)}):**")
    for f in conflicted:
        lines.append(f"- `{f}`")
    if skipped:
        lines.append("")
        lines.append(f"**Skipped (matched skip_paths) ({len(skipped)}):**")
        for f in skipped:
            lines.append(f"- `{f}`")
    if resolved:
        lines.append("")
        lines.append(f"**Resolved ({len(resolved)}):**")
        for f in resolved:
            lines.append(f"- `{f}`")

    if verify_result:
        lines.append("")
        lines.append("**Verify gate:**")
        lines.append("```")
        lines.append(verify_result.summary())
        lines.append("```")

    if failure:
        lines.append("")
        lines.append(f"**Did not push.** Reason: {failure}")
    elif pushed_sha:
        lines.append("")
        lines.append(f"Pushed resolution as `{pushed_sha[:12]}` (force-with-lease).")
        lines.append("Please review the merged content before merging.")

    return "\n".join(lines)


async def _resolve_one_file(
    repo_dir: Path, base_ref: str, file_path: str, llm_cfg: LLMConfig
) -> None:
    # PR-side intent: `git diff --merge-base <base> HEAD -- <file>`
    head_diff = await git_ops.diff_against_merge_base(repo_dir, "HEAD", base_ref, file_path)
    # Base-side intent: `git diff --merge-base HEAD <base> -- <file>`
    base_diff = await git_ops.diff_against_merge_base(repo_dir, base_ref, "HEAD", file_path)

    conflicted_content = await git_ops.read_conflicted_file(repo_dir, file_path)

    req = llm.ResolveRequest(
        repo_dir=repo_dir,
        file_path=file_path,
        head_diff=head_diff,
        base_diff=base_diff,
        conflicted_content=conflicted_content,
    )
    await llm.resolve_file(req, llm_cfg)

    # Verify the LLM actually removed the markers.
    if await git_ops.has_conflict_markers(repo_dir, file_path):
        raise llm.LLMError(f"{llm_cfg.backend} left conflict markers in {file_path}")


async def process_job(job: PRJob, cfg: Config, gh: GitHubClient) -> None:
    L = log.bind(
        delivery=job.delivery_id, owner=job.owner, repo=job.repo, pr=job.pr_number
    )
    repo_dir: Path | None = None
    pushed_sha: str | None = None
    failure: str | None = None
    conflicted: list[str] = []
    resolved: list[str] = []
    skipped: list[str] = []
    verify_result: verify.VerifyResult | None = None

    try:
        clone_url = await gh.clone_url(job.installation_id, job.owner, job.repo)
        spec = git_ops.CloneSpec(
            clone_url=clone_url,
            pr_branch=job.pr_branch,
            base_branch=job.base_branch,
            pr_head_sha=job.pr_head_sha,
            work_dir=cfg.work_dir,
        )
        repo_dir = await git_ops.clone_pr(
            spec, git_name=cfg.identity.git_name, git_email=cfg.identity.git_email
        )
        L = L.bind(repo_dir=str(repo_dir))

        override = load_repo_override(
            repo_dir,
            default_skip_paths=cfg.default_skip_paths,
            default_max_files_per_pr=cfg.default_max_files_per_pr,
        )
        if not override.enabled:
            L.info("disabled by repo config")
            return

        outcome = await git_ops.merge_base_into_head(repo_dir, job.base_branch)
        if outcome.clean:
            L.info("clean merge — nothing to do")
            return

        conflicted = list(outcome.conflicted_files)
        L.info("conflicts", count=len(conflicted), files=conflicted)

        # Compute the effective verify gate now so we can refuse to operate
        # if it's empty under strict mode (the gate is the only safety net).
        eff_verify = _effective_verify(cfg.verify, override)
        if cfg.require_repo_config and not (
            eff_verify.lint or eff_verify.typecheck or eff_verify.test
        ):
            failure = (
                "this repo has no verify gate configured. Add a "
                "`.pr-conflict-bot.toml` at the repo root with at least one of "
                "`[verify] lint`, `typecheck`, or `test` set — or set "
                "`[behavior] enabled = false` to opt this repo out. "
                "The bot refuses to push resolutions it can't verify."
            )
            L.warning("aborting: REQUIRE_REPO_CONFIG=true and verify gate is empty")
            return

        if len(conflicted) > override.max_files_per_pr:
            failure = (
                f"{len(conflicted)} conflicted files exceeds max_files_per_pr "
                f"({override.max_files_per_pr})"
            )
            L.warning("too many conflicts", failure=failure)
            return

        for f in conflicted:
            if _path_skipped(f, override):
                skipped.append(f)
                L.info("skip path", file=f)
                continue
            try:
                await _resolve_one_file(repo_dir, f"origin/{job.base_branch}", f, cfg.llm)
                resolved.append(f)
                L.info("resolved", file=f)
            except Exception as e:
                failure = f"resolution failed for `{f}`: {e}"
                L.exception("resolve failed", file=f)
                return

        if skipped:
            failure = (
                f"{len(skipped)} conflicted files were skipped by config — manual fix needed"
            )
            return

        # All conflicts resolved — commit and verify.
        commit_sha = await git_ops.stage_and_commit_resolution(
            repo_dir,
            f"merge {job.base_branch} into {job.pr_branch} — conflicts resolved by pr-conflict-bot",
        )
        L.info("committed", sha=commit_sha[:8])

        verify_result = await verify.run(eff_verify, repo_dir)
        if not verify_result.passed:
            failure = "verify gate failed — not pushing"
            L.warning("verify failed", summary=verify_result.summary())
            return

        await gh.dismiss_self_reviews(job.installation_id, job.owner, job.repo, job.pr_number)
        await git_ops.push_with_lease(repo_dir, job.pr_branch, job.pr_head_sha)
        pushed_sha = commit_sha
        L.info("pushed", sha=pushed_sha[:8])

    except Exception as e:
        failure = f"unexpected error: {e}"
        L.exception("job failed")
    finally:
        try:
            comment = _fmt_summary_comment(
                base_branch=job.base_branch,
                conflicted=conflicted,
                resolved=resolved,
                skipped=skipped,
                verify_result=verify_result,
                pushed_sha=pushed_sha,
                failure=failure,
            )
            await gh.post_issue_comment(
                job.installation_id, job.owner, job.repo, job.pr_number, comment
            )
        except Exception:
            L.exception("could not post summary comment")
        if repo_dir is not None:
            await git_ops.cleanup(repo_dir)


async def worker(
    queue: asyncio.Queue[PRJob], cfg: Config, gh: GitHubClient
) -> None:
    """Single worker — processes one job at a time. Run multiple instances for parallelism."""
    while True:
        job = await queue.get()
        try:
            await process_job(job, cfg, gh)
        except Exception:
            log.exception("worker top-level exception", delivery=job.delivery_id)
        finally:
            queue.task_done()
