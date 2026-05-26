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


def test_parse_findings_ignores_trailing_prose_with_brackets() -> None:
    raw = '[{"severity":"high","title":"t","detail":"d"}]\n\nNote [see issue 1].'
    assert parse_findings(raw) == [Finding("high", "t", "d")]


def test_parse_findings_missing_severity_defaults_to_medium() -> None:
    assert parse_findings('[{"title":"t","detail":"d"}]') == [Finding("medium", "t", "d")]


def test_parse_findings_null_fields_become_empty_strings() -> None:
    assert parse_findings('[{"severity":null,"title":null,"detail":null}]') == [
        Finding("medium", "", "")
    ]


def test_parse_findings_skips_non_dict_items() -> None:
    raw = '[1, "x", {"severity":"low","title":"t","detail":"d"}]'
    assert parse_findings(raw) == [Finding("low", "t", "d")]


def test_build_smoke_prompt_no_console_errors_shows_none() -> None:
    from pr_conflict_bot.qa.browse import PageState
    state = PageState(url="http://a", http_status=200, console_errors=(), text="ok", screenshot_path=None)
    assert "(none)" in build_smoke_prompt(state)
