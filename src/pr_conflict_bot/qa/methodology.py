"""QA prompt templates and findings parsing.

M1: a single 'smoke' judgment over one captured page. The model must reply
with a JSON array of findings so we can parse deterministically. Templates are
engine-neutral (no Claude-Code-only constructs) so any backend works.
"""

from __future__ import annotations

import json
import re

from .browse import PageState
from .report import Finding

_SMOKE_TEMPLATE = """\
You are a QA engineer reviewing a single web page. Report problems a real user
would hit. Be strict but do not invent issues.

PAGE URL: {url}
HTTP STATUS: {status}
CONSOLE ERRORS:
{console}

VISIBLE TEXT (truncated):
{text}

Respond with ONLY a JSON array of findings, each:
  {{"severity": "critical"|"high"|"medium"|"low", "title": "...", "detail": "..."}}
A non-200 status or any console error is at least "high". If the page looks
fine, respond with [].
"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_VALID_SEVERITIES = {"critical", "high", "medium", "low"}


def build_smoke_prompt(state: PageState) -> str:
    console = "\n".join(state.console_errors) if state.console_errors else "(none)"
    text = state.text[:2000]
    return _SMOKE_TEMPLATE.format(
        url=state.url, status=state.http_status, console=console, text=text
    )


def parse_findings(raw: str) -> list[Finding]:
    """Extract a findings list from model output. Tolerates fences/surrounding
    prose. Returns [] on anything unparseable — never raises."""
    candidate = raw
    fence = _FENCE_RE.search(raw)
    if fence:
        candidate = fence.group(1)
    else:
        arr = _ARRAY_RE.search(raw)
        if arr:
            candidate = arr.group(0)
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    findings: list[Finding] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "")).lower()
        if sev not in _VALID_SEVERITIES:
            sev = "medium"
        findings.append(
            Finding(severity=sev, title=str(item.get("title", "")), detail=str(item.get("detail", "")))
        )
    return findings
