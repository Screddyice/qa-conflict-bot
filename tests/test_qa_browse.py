from __future__ import annotations

import pytest

from pr_conflict_bot.qa.browse import FakeBrowse, PageState, SubprocessBrowse


async def test_fakebrowse_returns_canned_state() -> None:
    state = PageState(
        url="http://x", http_status=200, console_errors=("boom",), text="hi", screenshot_path=None
    )
    fb = FakeBrowse(state)
    got = await fb.capture("http://x")
    assert got is state
    assert fb.captured_urls == ["http://x"]


async def test_subprocessbrowse_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"http_status": 404, "console_errors": ["e1"], "text": "Not Found"}

    async def fake_run_json(cmd: list[str], *, timeout: float) -> dict[str, object]:
        assert cmd[0] == "/usr/local/bin/browse"
        assert "http://target" in cmd
        return payload

    sb = SubprocessBrowse(binary="/usr/local/bin/browse")
    monkeypatch.setattr(sb, "_run_json", fake_run_json)
    state = await sb.capture("http://target")

    assert state.url == "http://target"
    assert state.http_status == 404
    assert state.console_errors == ("e1",)
    assert state.text == "Not Found"
