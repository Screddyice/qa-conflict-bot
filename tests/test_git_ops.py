"""End-to-end tests against a real local git repo. Requires `git` on PATH."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from pr_conflict_bot import git_ops


def _run(args: list[str], cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True)


@pytest.fixture
def conflict_repo(tmp_path: Path) -> Path:
    """Build a tiny repo with a known merge conflict, return the clone path."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(["init", "-q", "-b", "main"], origin)
    _run(["config", "user.email", "test@x"], origin)
    _run(["config", "user.name", "Test"], origin)

    (origin / "f.txt").write_text("line one\nline two\nline three\n")
    _run(["add", "."], origin)
    _run(["commit", "-q", "-m", "init"], origin)

    _run(["checkout", "-q", "-b", "feature"], origin)
    (origin / "f.txt").write_text("line one\nFEATURE LINE\nline three\n")
    _run(["add", "."], origin)
    _run(["commit", "-q", "-m", "feature edit"], origin)

    _run(["checkout", "-q", "main"], origin)
    (origin / "f.txt").write_text("line one\nMAIN LINE\nline three\n")
    _run(["add", "."], origin)
    _run(["commit", "-q", "-m", "main edit"], origin)

    # Bare clone serves as origin for the bot's clone.
    bare = tmp_path / "bare.git"
    subprocess.check_call(["git", "clone", "--bare", str(origin), str(bare)])

    return bare


@pytest.mark.asyncio
async def test_merge_detects_conflict(conflict_repo: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    pr_head_sha = subprocess.check_output(
        ["git", "-C", str(conflict_repo), "rev-parse", "feature"], text=True
    ).strip()

    spec = git_ops.CloneSpec(
        clone_url=str(conflict_repo),
        pr_branch="feature",
        base_branch="main",
        pr_head_sha=pr_head_sha,
        work_dir=work,
    )
    repo = await git_ops.clone_pr(spec, git_name="t", git_email="t@x")
    try:
        outcome = await git_ops.merge_base_into_head(repo, "main")
        assert not outcome.clean
        assert outcome.conflicted_files == ("f.txt",)
        assert await git_ops.has_conflict_markers(repo, "f.txt")
    finally:
        shutil.rmtree(repo, ignore_errors=True)


@pytest.mark.asyncio
async def test_branch_not_found_raises_specific_exception(
    conflict_repo: Path, tmp_path: Path
) -> None:
    """When the PR branch is gone from origin, raise BranchNotFound (not a generic GitError).

    This lets the orchestrator distinguish "PR was closed/branch deleted before
    we got there" from real failures and skip silently instead of posting an
    alarming comment.
    """
    work = tmp_path / "work"
    work.mkdir()
    spec = git_ops.CloneSpec(
        clone_url=str(conflict_repo),
        pr_branch="branch-that-does-not-exist",
        base_branch="main",
        pr_head_sha="0" * 40,
        work_dir=work,
    )
    with pytest.raises(git_ops.BranchNotFound):
        await git_ops.clone_pr(spec, git_name="t", git_email="t@x")


@pytest.mark.asyncio
async def test_concurrent_clones_use_distinct_dirs(
    conflict_repo: Path, tmp_path: Path
) -> None:
    """Two concurrent clones with identical (clone_url, pr_branch) must not collide.

    Regression test for the deterministic-hash dir bug that caused Job B's
    rmtree to delete Job A's in-flight clone.
    """
    work = tmp_path / "work"
    work.mkdir()
    pr_head_sha = subprocess.check_output(
        ["git", "-C", str(conflict_repo), "rev-parse", "feature"], text=True
    ).strip()
    spec = git_ops.CloneSpec(
        clone_url=str(conflict_repo),
        pr_branch="feature",
        base_branch="main",
        pr_head_sha=pr_head_sha,
        work_dir=work,
    )
    repos = await asyncio.gather(
        git_ops.clone_pr(spec, git_name="t", git_email="t@x"),
        git_ops.clone_pr(spec, git_name="t", git_email="t@x"),
    )
    try:
        assert repos[0] != repos[1]
        assert (repos[0] / ".git").is_dir()
        assert (repos[1] / ".git").is_dir()
    finally:
        for r in repos:
            shutil.rmtree(r, ignore_errors=True)


@pytest.mark.asyncio
async def test_diffs_against_merge_base(conflict_repo: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    pr_head_sha = subprocess.check_output(
        ["git", "-C", str(conflict_repo), "rev-parse", "feature"], text=True
    ).strip()

    spec = git_ops.CloneSpec(
        clone_url=str(conflict_repo),
        pr_branch="feature",
        base_branch="main",
        pr_head_sha=pr_head_sha,
        work_dir=work,
    )
    repo = await git_ops.clone_pr(spec, git_name="t", git_email="t@x")
    try:
        head_diff = await git_ops.diff_against_merge_base(repo, "HEAD", "origin/main", "f.txt")
        base_diff = await git_ops.diff_against_merge_base(repo, "origin/main", "HEAD", "f.txt")
        # PR-side mentions FEATURE; base-side mentions MAIN.
        assert "FEATURE" in head_diff
        assert "MAIN" in base_diff
        assert "FEATURE" not in base_diff
        assert "MAIN" not in head_diff
    finally:
        shutil.rmtree(repo, ignore_errors=True)
