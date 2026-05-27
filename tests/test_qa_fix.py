from __future__ import annotations

from pathlib import Path

import pytest

from pr_conflict_bot.config import (
    BotIdentity,
    Config,
    GitHubAppConfig,
    LLMConfig,
    VerifyConfig,
)
from pr_conflict_bot.qa import orchestrator as orch
from pr_conflict_bot.qa.browse import PageState
from pr_conflict_bot.qa.methodology import build_fix_prompt
from pr_conflict_bot.qa.report import Finding
from pr_conflict_bot.server import PRJob


def _cfg() -> Config:
    return Config(
        github=GitHubAppConfig(app_id=1, private_key_pem="x", webhook_secret="s", bot_login="b[bot]"),
        llm=LLMConfig(backend="claude"), verify=VerifyConfig(test="pytest"), identity=BotIdentity(),
        listen_host="127.0.0.1", listen_port=8081, webhook_path="/hooks/github",
        work_dir=Path("/tmp"), log_level="INFO",
    )


def _job() -> PRJob:
    return PRJob(
        delivery_id="d", installation_id=1, owner="o", repo="r", pr_number=7,
        pr_branch="feat", base_branch="main", pr_head_sha="abcdef123456", sender_login="u", sender_type="User",
    )


class _GH:
    """default_run_fix no longer talks to GitHub (it pushes to the PR branch via
    git), so the fake just needs to exist to satisfy the call signature."""


_STATE = PageState("http://app", 500, ("E",), "err", None)
_FINDINGS = [Finding("high", "broken button", "click does nothing")]


def test_build_fix_prompt_lists_findings_and_url() -> None:
    p = build_fix_prompt(_STATE, _FINDINGS)
    assert "http://app" in p
    assert "broken button" in p
    assert "Edit the source files" in p


async def test_default_run_fix_no_edits_no_push(monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[int] = []

    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        return None

    async def fake_has_changes(repo_dir):  # type: ignore[no-untyped-def]
        return False

    async def fake_push(repo_dir, branch, expected_remote_sha):  # type: ignore[no-untyped-def]
        pushed.append(1)

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    monkeypatch.setattr(orch.git_ops, "has_changes", fake_has_changes)
    monkeypatch.setattr(orch.git_ops, "push_with_lease", fake_push)
    out = await orch.default_run_fix(_job(), _cfg(), _GH(), Path("/tmp/x"), "fix prompt", _FINDINGS)  # type: ignore[arg-type]
    assert out.changed is False
    assert out.pushed is False
    assert pushed == []


async def test_default_run_fix_refuses_without_verify_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # cfg with an EMPTY verify gate — fix mode must not push (empty gate trivially
    # passes), and must not even spend an LLM edit.
    edited: list[int] = []

    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        edited.append(1)

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    cfg = Config(
        github=GitHubAppConfig(app_id=1, private_key_pem="x", webhook_secret="s", bot_login="b[bot]"),
        llm=LLMConfig(), verify=VerifyConfig(), identity=BotIdentity(),  # empty verify
        listen_host="127.0.0.1", listen_port=8081, webhook_path="/hooks/github",
        work_dir=Path("/tmp"), log_level="INFO",
    )
    out = await orch.default_run_fix(_job(), cfg, _GH(), Path("/tmp/x"), "fix prompt", _FINDINGS)  # type: ignore[arg-type]
    assert out.changed is False
    assert out.pushed is False
    assert edited == []  # didn't even attempt the edit


async def test_default_run_fix_verify_fail_no_push(monkeypatch: pytest.MonkeyPatch) -> None:
    from pr_conflict_bot.verify import StepResult, VerifyResult

    pushed: list[int] = []

    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        return None

    async def fake_has_changes(repo_dir):  # type: ignore[no-untyped-def]
        return True

    async def fake_verify(cfg, repo_dir):  # type: ignore[no-untyped-def]
        return VerifyResult(passed=False, steps=(StepResult("test", "pytest", False, False, "boom"),))

    async def fake_push(repo_dir, branch, expected_remote_sha):  # type: ignore[no-untyped-def]
        pushed.append(1)

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    monkeypatch.setattr(orch.git_ops, "has_changes", fake_has_changes)
    monkeypatch.setattr(orch.verify, "run", fake_verify)
    monkeypatch.setattr(orch.git_ops, "push_with_lease", fake_push)
    out = await orch.default_run_fix(_job(), _cfg(), _GH(), Path("/tmp/x"), "fix prompt", _FINDINGS)  # type: ignore[arg-type]
    assert out.changed is True
    assert out.verified is False
    assert out.pushed is False
    assert pushed == []  # never pushed unverified edits


async def test_default_run_fix_success_pushes_to_pr_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from pr_conflict_bot.verify import VerifyResult

    steps: list[str] = []
    pushed: dict[str, str] = {}

    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        steps.append("edit")

    async def fake_has_changes(repo_dir):  # type: ignore[no-untyped-def]
        return True

    async def fake_verify(cfg, repo_dir):  # type: ignore[no-untyped-def]
        return VerifyResult(passed=True, steps=())

    async def fake_commit(repo_dir, message):  # type: ignore[no-untyped-def]
        steps.append(f"commit:{message}")
        return "newsha"

    async def fake_push(repo_dir, branch, expected_remote_sha):  # type: ignore[no-untyped-def]
        steps.append("push")
        pushed.update(branch=branch, sha=expected_remote_sha)

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    monkeypatch.setattr(orch.git_ops, "has_changes", fake_has_changes)
    monkeypatch.setattr(orch.verify, "run", fake_verify)
    monkeypatch.setattr(orch.git_ops, "stage_and_commit_resolution", fake_commit)
    monkeypatch.setattr(orch.git_ops, "push_with_lease", fake_push)

    out = await orch.default_run_fix(_job(), _cfg(), _GH(), Path("/tmp/x"), "fix prompt", _FINDINGS)  # type: ignore[arg-type]
    assert out.verified is True
    assert out.pushed is True
    # Pushed onto the PR's OWN branch, lease-guarded by the PR head sha.
    assert pushed == {"branch": "feat", "sha": "abcdef123456"}
    assert steps == ["edit", "commit:fix(qa): address 1 QA finding(s) on #7", "push"]


async def test_default_run_fix_push_rejected_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # A concurrent human push (or a fork PR / branch protection) makes the
    # lease-guarded push fail. We must report it, not swallow it.
    from pr_conflict_bot.verify import VerifyResult

    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        return None

    async def fake_has_changes(repo_dir):  # type: ignore[no-untyped-def]
        return True

    async def fake_verify(cfg, repo_dir):  # type: ignore[no-untyped-def]
        return VerifyResult(passed=True, steps=())

    async def fake_commit(repo_dir, message):  # type: ignore[no-untyped-def]
        return "newsha"

    async def fake_push(repo_dir, branch, expected_remote_sha):  # type: ignore[no-untyped-def]
        raise orch.git_ops.GitError("! [rejected] (stale info)")

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    monkeypatch.setattr(orch.git_ops, "has_changes", fake_has_changes)
    monkeypatch.setattr(orch.verify, "run", fake_verify)
    monkeypatch.setattr(orch.git_ops, "stage_and_commit_resolution", fake_commit)
    monkeypatch.setattr(orch.git_ops, "push_with_lease", fake_push)

    out = await orch.default_run_fix(_job(), _cfg(), _GH(), Path("/tmp/x"), "fix prompt", _FINDINGS)  # type: ignore[arg-type]
    assert out.changed is True
    assert out.verified is True
    assert out.pushed is False
    assert "could not be pushed" in out.detail
