from __future__ import annotations

from pathlib import Path

import pytest

from pr_conflict_bot.qa.browse import (
    BrowseError,
    FakeBrowse,
    PageState,
    SubprocessBrowse,
    parse_console_errors,
    parse_nav_status,
    unwrap_untrusted,
)

# Exact output strings emitted by the real gstack `browse` CLI (captured by
# driving the vendored binary against a local server). The parser contract is
# pinned to these so a CLI change surfaces as a test failure, not silent drift.
_GOTO_OK = "[browse] Starting server...\nNavigated to http://127.0.0.1:5000 (200)\n"
_GOTO_404 = "Navigated to http://127.0.0.1:5000/nope (404)\n"
_TEXT_OUT = (
    "--- BEGIN UNTRUSTED EXTERNAL CONTENT (source: http://127.0.0.1:5000/) ---\n"
    "Hello QASome body text here.\n"
    "--- END UNTRUSTED EXTERNAL CONTENT ---\n"
)
_CONSOLE_WITH_ERRORS = (
    "--- BEGIN UNTRUSTED EXTERNAL CONTENT (source: http://127.0.0.1:5000/) ---\n"
    "[2026-05-26T07:53:03.550Z] [error] BOOM synthetic error\n"
    "[2026-05-26T07:53:03.551Z] [error] second error\n"
    "--- END UNTRUSTED EXTERNAL CONTENT ---\n"
)
_CONSOLE_CLEAN = (
    "--- BEGIN UNTRUSTED EXTERNAL CONTENT (source: http://127.0.0.1:5000/) ---\n"
    "(no console errors)\n"
    "--- END UNTRUSTED EXTERNAL CONTENT ---\n"
)


# --- parser helpers (pinned to real CLI output) -----------------------------


def test_parse_nav_status_extracts_code() -> None:
    assert parse_nav_status(_GOTO_OK) == 200
    assert parse_nav_status(_GOTO_404) == 404


def test_parse_nav_status_missing_is_zero() -> None:
    assert parse_nav_status("could not connect\n") == 0


def test_unwrap_untrusted_strips_markers() -> None:
    assert unwrap_untrusted(_TEXT_OUT) == "Hello QASome body text here."


def test_unwrap_untrusted_without_markers_passes_through() -> None:
    assert unwrap_untrusted("  plain text  ") == "plain text"


def test_parse_console_errors_extracts_messages() -> None:
    assert parse_console_errors(_CONSOLE_WITH_ERRORS) == (
        "BOOM synthetic error",
        "second error",
    )


def test_parse_console_errors_clean_is_empty() -> None:
    assert parse_console_errors(_CONSOLE_CLEAN) == ()


# --- FakeBrowse --------------------------------------------------------------


async def test_fakebrowse_returns_canned_state() -> None:
    state = PageState(
        url="http://x", http_status=200, console_errors=("boom",), text="hi", screenshot_path=None
    )
    fb = FakeBrowse(state)
    got = await fb.capture("http://x")
    assert got is state
    assert fb.captured_urls == ["http://x"]


# --- SubprocessBrowse (against a fake `browse` command sequence) -------------


def _stub_browse(
    monkeypatch: pytest.MonkeyPatch,
    sb: SubprocessBrowse,
    outputs: dict[str, str],
    *,
    calls: list[list[str]] | None = None,
    screenshot_writes: Path | None = None,
) -> None:
    """Patch SubprocessBrowse._run_text to return canned per-subcommand output."""

    async def fake_run_text(args: list[str], *, timeout: float) -> str:
        if calls is not None:
            calls.append(args)
        if args[0] == "screenshot" and screenshot_writes is not None:
            screenshot_writes.write_bytes(b"PNG")
        return outputs.get(args[0], "")

    monkeypatch.setattr(sb, "_run_text", fake_run_text)


async def test_capture_runs_goto_text_console_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sb = SubprocessBrowse(binary="/usr/local/bin/browse")
    calls: list[list[str]] = []
    _stub_browse(
        monkeypatch,
        sb,
        {"goto": _GOTO_404, "text": _TEXT_OUT, "console": _CONSOLE_WITH_ERRORS},
        calls=calls,
    )

    state = await sb.capture("http://target")

    assert [c[0] for c in calls] == ["goto", "text", "console"]
    assert calls[0] == ["goto", "http://target"]
    assert calls[2] == ["console", "--errors"]
    assert state.url == "http://target"
    assert state.http_status == 404
    assert state.text == "Hello QASome body text here."
    assert state.console_errors == ("BOOM synthetic error", "second error")
    assert state.screenshot_path is None


async def test_capture_takes_screenshot_when_path_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sb = SubprocessBrowse(binary="/b/browse")
    calls: list[list[str]] = []
    shot = tmp_path / "s.png"
    _stub_browse(
        monkeypatch,
        sb,
        {"goto": _GOTO_OK, "text": _TEXT_OUT, "console": _CONSOLE_CLEAN},
        calls=calls,
        screenshot_writes=shot,
    )

    state = await sb.capture("http://t", screenshot_to=shot)

    assert ["screenshot", str(shot)] in calls
    assert state.screenshot_path == shot
    assert state.console_errors == ()


async def test_capture_screenshot_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sb = SubprocessBrowse(binary="/b/browse")
    shot = tmp_path / "blocked.png"

    async def fake_run_text(args: list[str], *, timeout: float) -> str:
        if args[0] == "screenshot":
            raise BrowseError("Path must be within: /private/tmp")
        return {"goto": _GOTO_OK, "text": _TEXT_OUT, "console": _CONSOLE_CLEAN}[args[0]]

    monkeypatch.setattr(sb, "_run_text", fake_run_text)

    state = await sb.capture("http://t", screenshot_to=shot)

    # Capture still succeeds; screenshot just degrades to None.
    assert state.screenshot_path is None
    assert state.http_status == 200
    assert state.text == "Hello QASome body text here."
