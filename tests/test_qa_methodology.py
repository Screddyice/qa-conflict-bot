from __future__ import annotations

from pr_conflict_bot.qa.browse import PageState
from pr_conflict_bot.qa.methodology import build_smoke_prompt, parse_findings
from pr_conflict_bot.qa.report import Finding


def _state() -> PageState:
    return PageState(
        url="http://app", http_status=500, console_errors=("TypeError: x",), text="Error", screenshot_path=None
    )


def test_prompt_includes_page_state_and_json_instruction() -> None:
    p = build_smoke_prompt(_state())
    assert "http://app" in p
    assert "500" in p
    assert "TypeError: x" in p
    assert "JSON" in p  # must instruct a JSON array response


def test_parse_findings_valid_json() -> None:
    raw = '[{"severity": "high", "title": "500 error", "detail": "page returns 500"}]'
    findings = parse_findings(raw)
    assert findings == [Finding("high", "500 error", "page returns 500")]


def test_parse_findings_tolerates_fenced_and_surrounding_text() -> None:
    raw = 'Here you go:\n```json\n[{"severity":"low","title":"t","detail":"d"}]\n```\nDone.'
    assert parse_findings(raw) == [Finding("low", "t", "d")]


def test_parse_findings_empty_or_garbage_returns_empty() -> None:
    assert parse_findings("no findings") == []
    assert parse_findings("[]") == []
