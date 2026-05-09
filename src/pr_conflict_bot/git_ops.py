"""Git operations: clone, configure, merge-and-detect-conflicts, push --force-with-lease."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


class BranchNotFound(GitError):
    """The PR's head branch no longer exists on origin (PR closed or branch deleted before we cloned)."""


@dataclass(frozen=True)
class MergeOutcome:
    clean: bool
    conflicted_files: tuple[str, ...]


@dataclass(frozen=True)
class CloneSpec:
    """All inputs needed to materialize a PR working tree."""
    clone_url: str            # https://x-access-token:<token>@github.com/<owner>/<repo>.git
    pr_branch: str            # head ref, e.g. "feat/foo"
    base_branch: str          # base ref, e.g. "main"
    pr_head_sha: str          # full SHA the PR is currently at (used as the lease guard)
    work_dir: Path            # parent dir; we'll create work_dir/<repo>-<pr-number>


async def _run(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise GitError(f"timeout after {timeout}s: {' '.join(cmd)}") from e
    out = stdout_b.decode(errors="replace")
    err = stderr_b.decode(errors="replace")
    if check and proc.returncode != 0:
        raise GitError(
            f"git command failed ({proc.returncode}): {' '.join(cmd)}\nstdout:\n{out}\nstderr:\n{err}"
        )
    return proc.returncode or 0, out, err


async def clone_pr(spec: CloneSpec, *, git_name: str, git_email: str) -> Path:
    """Shallow-ish clone of the PR branch with the base also fetched. Returns the repo root."""
    spec.work_dir.mkdir(parents=True, exist_ok=True)
    # Per-job unique dir. A deterministic hash of (clone_url, pr_branch) raced
    # whenever two webhook events for the same PR fired near-simultaneously, or
    # two PRs shared a branch name — Job B's rmtree would nuke Job A's in-flight
    # clone.
    repo_dir = spec.work_dir / f"clone-{uuid.uuid4().hex}"

    try:
        await _run(
            ["git", "clone", "--no-tags", "--branch", spec.pr_branch, spec.clone_url, str(repo_dir)]
        )
    except GitError as e:
        shutil.rmtree(repo_dir, ignore_errors=True)
        msg = str(e)
        # Branch was deleted between webhook arrival and clone — the PR was
        # closed or merged. Surface this distinctly so the orchestrator can
        # skip silently instead of posting an "unexpected error" comment.
        if "Remote branch" in msg and "not found in upstream origin" in msg:
            raise BranchNotFound(
                f"PR branch '{spec.pr_branch}' no longer exists on origin"
            ) from e
        raise
    # Configure identity, conflict style, and rerere for cache reuse across re-runs.
    for args in [
        ["config", "user.name", git_name],
        ["config", "user.email", git_email],
        ["config", "merge.conflictStyle", "zdiff3"],
        ["config", "rerere.enabled", "true"],
        # Make sure pulls/merges don't auto-rebase.
        ["config", "pull.rebase", "false"],
    ]:
        await _run(["git", *args], cwd=repo_dir)

    # Fetch the base branch so we can merge it in.
    await _run(["git", "fetch", "origin", spec.base_branch], cwd=repo_dir)

    # Sanity: confirm we're at the expected head SHA. If not, the PR was updated mid-flight.
    _, sha_out, _ = await _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    sha = sha_out.strip()
    if sha != spec.pr_head_sha:
        raise GitError(
            f"clone is at {sha}, expected PR head {spec.pr_head_sha} — PR moved, abort and retry"
        )
    return repo_dir


async def merge_base_into_head(repo_dir: Path, base_branch: str) -> MergeOutcome:
    """Try to merge origin/<base_branch> into the current branch.

    Returns clean=True if no conflicts (merge committed). Returns clean=False with the
    list of conflicted file paths if conflicts occurred (merge left in conflicted state).
    """
    rc, out, err = await _run(
        ["git", "merge", "--no-edit", "--no-ff", f"origin/{base_branch}"],
        cwd=repo_dir,
        check=False,
    )
    if rc == 0:
        return MergeOutcome(clean=True, conflicted_files=())

    # Conflict: list the unmerged paths.
    _, files_out, _ = await _run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir
    )
    files = tuple(line.strip() for line in files_out.splitlines() if line.strip())
    if not files:
        # Merge failed for some reason other than conflict — surface the error.
        raise GitError(f"git merge failed without unmerged files:\nstdout:\n{out}\nstderr:\n{err}")
    return MergeOutcome(clean=False, conflicted_files=files)


async def stage_and_commit_resolution(repo_dir: Path, message: str) -> str:
    """Stage all resolved files and create the merge commit. Returns the new commit SHA."""
    await _run(["git", "add", "-A"], cwd=repo_dir)
    # Make sure no unmerged paths remain.
    _, unmerged, _ = await _run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir
    )
    if unmerged.strip():
        raise GitError(f"unmerged paths remain after resolution: {unmerged.strip()}")
    await _run(["git", "commit", "--no-edit", "-m", message], cwd=repo_dir)
    _, sha_out, _ = await _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    return sha_out.strip()


async def push_with_lease(
    repo_dir: Path, branch: str, expected_remote_sha: str
) -> None:
    """Push current HEAD to origin/<branch>, only if the remote is still at expected_remote_sha."""
    await _run(
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/{branch}:{expected_remote_sha}",
            "origin",
            f"HEAD:refs/heads/{branch}",
        ],
        cwd=repo_dir,
    )


async def diff_against_merge_base(
    repo_dir: Path, side: str, base_ref: str, file_path: str
) -> str:
    """Return `git diff --merge-base <base_ref> <side> -- <file>`.

    side="HEAD" gives the PR's intent on the file; side=base_ref gives main's intent.
    """
    _, out, _ = await _run(
        ["git", "diff", "--merge-base", base_ref, side, "--", file_path],
        cwd=repo_dir,
    )
    return out


async def read_conflicted_file(repo_dir: Path, file_path: str) -> str:
    full = repo_dir / file_path
    if not full.is_file():
        raise GitError(f"expected conflicted file does not exist: {file_path}")
    return full.read_text()


async def has_conflict_markers(repo_dir: Path, file_path: str) -> bool:
    content = (repo_dir / file_path).read_text(errors="replace")
    # zdiff3 markers: <<<<<<<, |||||||, =======, >>>>>>>
    return any(
        marker in content
        for marker in ("<<<<<<<", "|||||||", "=======", ">>>>>>>")
    )


async def cleanup(repo_dir: Path) -> None:
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
