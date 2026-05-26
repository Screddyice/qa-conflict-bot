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

_FIX_TEMPLATE = """\
You are a senior engineer fixing QA findings on a web app. The app's code is in
the current working directory. A QA pass found these issues on {url} (HTTP {status}):

{findings}

Console errors observed:
{console}

Edit the source files in this directory to fix these issues. Make the smallest
change that genuinely resolves each finding. Do NOT change unrelated code, do NOT
edit tests to make them pass, and do NOT add comments narrating the fix. If a
finding is not actually fixable from the code (e.g. an external service is down),
leave it. When done, the working tree should contain only the fix edits.
"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_VALID_SEVERITIES = {"critical", "high", "medium", "low"}


def build_fix_prompt(state: PageState, findings: list[Finding]) -> str:
    """Editing prompt for fix mode: instruct the backend to edit the repo to
    address the findings. Engine-neutral (works for claude -p and codex exec)."""
    listed = "\n".join(f"- [{f.severity}] {f.title}: {f.detail}" for f in findings)
    console = "\n".join(state.console_errors) if state.console_errors else "(none)"
    return _FIX_TEMPLATE.format(
        url=state.url, status=state.http_status, findings=listed, console=console
    )


def _extract_array_text(raw: str) -> str | None:
    """Return the JSON-array substring from model output, or None.

    Prefers a fenced ```json block; otherwise scans from the first '[' with a
    JSON decoder so trailing prose (even prose containing brackets) is ignored.
    """
    fence = _FENCE_RE.search(raw)
    if fence:
        return fence.group(1)
    start = raw.find("[")
    if start == -1:
        return None
    try:
        _obj, end = json.JSONDecoder().raw_decode(raw[start:])
    except ValueError:
        return None
    return raw[start : start + end]


def build_smoke_prompt(state: PageState) -> str:
    console = "\n".join(state.console_errors) if state.console_errors else "(none)"
    text = state.text[:2000]
    return _SMOKE_TEMPLATE.format(
        url=state.url, status=state.http_status, console=console, text=text
    )


def parse_findings(raw: str) -> list[Finding]:
    """Extract a findings list from model output. Tolerates fences/surrounding
    prose (incl. trailing prose containing brackets). Returns [] on anything
    unparseable — never raises."""
    candidate = _extract_array_text(raw)
    if candidate is None:
        return []
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
        sev = str(item.get("severity") or "").lower()
        if sev not in _VALID_SEVERITIES:
            sev = "medium"
        findings.append(
            Finding(
                severity=sev,
                title=str(item.get("title") or ""),
                detail=str(item.get("detail") or ""),
            )
        )
    return findings
