from __future__ import annotations

from pathlib import Path

import pytest

from pr_conflict_bot import llm
from pr_conflict_bot.config import LLMConfig


async def test_complete_claude_builds_command(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_capture(cmd: list[str], *, cwd: Path | None, env: dict[str, str], timeout: float) -> str:
        captured["cmd"] = cmd
        captured["env"] = env
        return "MODEL SAYS HELLO"

    monkeypatch.setattr(llm, "_run_capture", fake_run_capture)
    out = await llm.complete("judge this", LLMConfig(backend="claude", oauth_token="tok"))

    assert out == "MODEL SAYS HELLO"
    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert cmd[-1] == "judge this"
    assert captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"


async def test_complete_codex_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_capture(cmd: list[str], *, cwd: Path | None, env: dict[str, str], timeout: float) -> str:
        captured["cmd"] = cmd
        return "ok"

    monkeypatch.setattr(llm, "_run_capture", fake_run_capture)
    await llm.complete("judge this", LLMConfig(backend="codex"))

    cmd = captured["cmd"]
    assert cmd[0] == "codex"
    assert "exec" in cmd
    assert "read-only" in cmd  # never let a judgment call edit files


async def test_complete_unknown_backend_raises() -> None:
    with pytest.raises(llm.LLMError):
        await llm.complete("x", LLMConfig(backend="bogus"))
