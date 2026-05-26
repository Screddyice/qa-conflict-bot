"""QA findings model, health score, and PR-comment formatting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

_SEVERITY_WEIGHT: dict[str, float] = {
    "critical": 4.0,
    "high": 2.0,
    "medium": 1.0,
    "low": 0.3,
}


@dataclass(frozen=True)
class Finding:
    severity: str  # "critical" | "high" | "medium" | "low"
    title: str
    detail: str


def health_score(findings: Sequence[Finding]) -> float:
    """10.0 minus severity-weighted penalties, clamped to [0, 10], 1 decimal."""
    penalty = sum(_SEVERITY_WEIGHT.get(f.severity.lower(), 1.0) for f in findings)
    return round(max(0.0, 10.0 - penalty), 1)


def format_comment(
    *,
    url: str,
    findings: Sequence[Finding],
    score: float,
    failure: str | None,
) -> str:
    lines = ["**pr-conflict-bot: QA** (report-only)", ""]
    if failure:
        lines.append(f"**Did not run.** {failure}")
        return "\n".join(lines)

    lines.append(f"Tested `{url}` — health **{score:.1f}/10**.")
    lines.append("")
    if not findings:
        lines.append("No issues found. ✅")
        return "\n".join(lines)

    lines.append(f"**Findings ({len(findings)}):**")
    for f in findings:
        lines.append(f"- **[{f.severity}]** {f.title} — {f.detail}")
    lines.append("")
    lines.append("_Report-only: no code was changed. Review and fix as needed._")
    return "\n".join(lines)
