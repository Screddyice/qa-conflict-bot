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
    def __init__(self) -> None:
        self.created: list[dict[str, str]] = []

    async def create_pull_request(self, installation_id, owner, repo, *, head, base, title, body):  # type: ignore[no-untyped-def]
        self.created.append({"head": head, "base": base, "title": title, "body": body})
        return "https://github.com/o/r/pull/123"


_STATE = PageState("http://app", 500, ("E",), "err", None)
_FINDINGS = [Finding("high", "broken button", "click does nothing")]


def test_build_fix_prompt_lists_findings_and_url() -> None:
    p = build_fix_prompt(_STATE, _FINDINGS)
    assert "http://app" in p
    assert "broken button" in p
    assert "Edit the source files" in p


async def test_default_run_fix_no_edits_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        return None

    async def fake_has_changes(repo_dir):  # type: ignore[no-untyped-def]
        return False

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    monkeypatch.setattr(orch.git_ops, "has_changes", fake_has_changes)
    gh = _GH()
    out = await orch.default_run_fix(_job(), _cfg(), gh, Path("/tmp/x"), _STATE, _FINDINGS)  # type: ignore[arg-type]
    assert out.changed is False
    assert out.pr_url is None
    assert gh.created == []


async def test_default_run_fix_refuses_without_verify_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # cfg with an EMPTY verify gate — fix mode must not open a PR (empty gate
    # trivially passes), and must not even spend an LLM edit.
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
    gh = _GH()
    out = await orch.default_run_fix(_job(), cfg, gh, Path("/tmp/x"), _STATE, _FINDINGS)  # type: ignore[arg-type]
    assert out.changed is False
    assert out.pr_url is None
    assert gh.created == []
    assert edited == []  # didn't even attempt the edit


async def test_default_run_fix_verify_fail_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    from pr_conflict_bot.verify import StepResult, VerifyResult

    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        return None

    async def fake_has_changes(repo_dir):  # type: ignore[no-untyped-def]
        return True

    async def fake_verify(cfg, repo_dir):  # type: ignore[no-untyped-def]
        return VerifyResult(passed=False, steps=(StepResult("test", "pytest", False, False, "boom"),))

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    monkeypatch.setattr(orch.git_ops, "has_changes", fake_has_changes)
    monkeypatch.setattr(orch.verify, "run", fake_verify)
    gh = _GH()
    out = await orch.default_run_fix(_job(), _cfg(), gh, Path("/tmp/x"), _STATE, _FINDINGS)  # type: ignore[arg-type]
    assert out.changed is True
    assert out.verified is False
    assert out.pr_url is None
    assert gh.created == []


async def test_default_run_fix_success_opens_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    from pr_conflict_bot.verify import VerifyResult

    steps: list[str] = []

    async def fake_apply_edit(prompt, cfg, *, cwd, timeout=600.0):  # type: ignore[no-untyped-def]
        steps.append("edit")

    async def fake_has_changes(repo_dir):  # type: ignore[no-untyped-def]
        return True

    async def fake_verify(cfg, repo_dir):  # type: ignore[no-untyped-def]
        return VerifyResult(passed=True, steps=())

    async def fake_create_branch(repo_dir, branch):  # type: ignore[no-untyped-def]
        steps.append(f"branch:{branch}")

    async def fake_commit(repo_dir, message):  # type: ignore[no-untyped-def]
        steps.append("commit")
        return "sha"

    async def fake_push(repo_dir, branch):  # type: ignore[no-untyped-def]
        steps.append(f"push:{branch}")

    monkeypatch.setattr(orch.llm, "apply_edit", fake_apply_edit)
    monkeypatch.setattr(orch.git_ops, "has_changes", fake_has_changes)
    monkeypatch.setattr(orch.verify, "run", fake_verify)
    monkeypatch.setattr(orch.git_ops, "create_branch", fake_create_branch)
    monkeypatch.setattr(orch.git_ops, "stage_and_commit_resolution", fake_commit)
    monkeypatch.setattr(orch.git_ops, "push_new_branch", fake_push)

    gh = _GH()
    out = await orch.default_run_fix(_job(), _cfg(), gh, Path("/tmp/x"), _STATE, _FINDINGS)  # type: ignore[arg-type]
    assert out.verified is True
    assert out.pr_url == "https://github.com/o/r/pull/123"
    assert gh.created[0]["base"] == "feat"  # fix PR targets the original PR branch
    assert gh.created[0]["head"] == "qa-fix/feat-abcdef12"
    assert "broken button" in gh.created[0]["body"]
    assert steps == ["edit", "branch:qa-fix/feat-abcdef12", "commit", "push:qa-fix/feat-abcdef12"]
