from __future__ import annotations

from pr_conflict_bot.qa.report import Finding, format_comment, health_score


def test_health_score_no_findings_is_ten() -> None:
    assert health_score([]) == 10.0


def test_health_score_weights_and_clamps() -> None:
    findings = [Finding("critical", "a", "d"), Finding("low", "b", "d")]
    # 10 - 4.0 (critical) - 0.3 (low) = 5.7
    assert health_score(findings) == 5.7
    # floor at 0
    many = [Finding("critical", str(i), "d") for i in range(5)]
    assert health_score(many) == 0.0


def test_format_comment_lists_findings_and_score() -> None:
    out = format_comment(
        url="http://x",
        findings=[Finding("high", "Button dead", "click does nothing")],
        score=8.0,
        failure=None,
    )
    assert "pr-conflict-bot: QA" in out
    assert "http://x" in out
    assert "8.0/10" in out
    assert "Button dead" in out
    assert "high" in out


def test_format_comment_failure_path() -> None:
    out = format_comment(url="", findings=[], score=0.0, failure="no QA url configured")
    assert "Did not run" in out
    assert "no QA url configured" in out


def test_format_comment_no_findings_success_branch() -> None:
    out = format_comment(url="http://x", findings=[], score=10.0, failure=None)
    assert "No issues found" in out
    assert "10.0/10" in out


def test_health_score_unknown_severity_defaults_to_one() -> None:
    # an out-of-vocabulary severity is weighted as 1.0 (same as "medium")
    assert health_score([Finding("bogus", "t", "d")]) == 9.0
