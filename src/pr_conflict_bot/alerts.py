"""Failure categorization + edge-triggered ops alerts.

Two concerns, both born from the Jun 2026 outage where the claude OAuth token
was revoked and every resolution 401'd for five days before anyone noticed:

- The PR comment must say WHY resolution failed without leaking raw CLI
  output (stdout/stderr can carry env material). `categorize` maps an
  exception to a stable category; `public_reason` is the sanitized one-liner
  that goes in the comment. Full detail stays in journald.

- `Alerter` posts to a Slack webhook only on the ok->fail transition (and a
  recovery note on fail->ok) — never on every chronic failure. State is a
  JSON file so the edge survives bot restarts. With no webhook configured it
  is a no-op.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp
import structlog

from .llm import LLMError

log = structlog.get_logger()

SendFn = Callable[[str], Awaitable[None]]

_AUTH_PAT = re.compile(
    r"401|invalid authentication|failed to authenticate|oauth token|not logged in|/login",
    re.IGNORECASE,
)
_TIMEOUT_PAT = re.compile(r"timed out", re.IGNORECASE)


def categorize(exc: BaseException) -> str:
    """Stable, operator-facing failure category. Never raises."""
    if isinstance(exc, LLMError):
        msg = str(exc)
        if _AUTH_PAT.search(msg):
            return "auth failure"
        if _TIMEOUT_PAT.search(msg):
            return "timeout"
        return "LLM error"
    return "unexpected error"


_REASON_HINTS = {
    "auth failure": (
        "the bot could not authenticate to its LLM backend; "
        "an operator needs to refresh its credentials"
    ),
    "timeout": "the LLM did not finish in time",
    "LLM error": "the LLM step errored (details in server logs)",
    "unexpected error": "details in server logs",
}


def public_reason(file_path: str, exc: BaseException) -> str:
    """One-line, PR-safe reason. Deliberately excludes the exception text."""
    category = categorize(exc)
    return f"resolution failed for `{file_path}` — {category}: {_REASON_HINTS[category]}."


class Alerter:
    """Edge-triggered alert sender with file-backed state.

    record_failure/record_success return whether a message was actually sent,
    so callers can log the edge. Alert delivery must never take the job down:
    send errors are logged and swallowed.
    """

    def __init__(self, state_path: Path, send: SendFn | None) -> None:
        self._state_path = state_path
        self._send = send

    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _store(self, state: dict[str, str]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state))
        except OSError:
            log.exception("alert state write failed", path=str(self._state_path))

    async def _post(self, text: str) -> bool:
        if self._send is None:
            return False
        try:
            await self._send(text)
            return True
        except Exception:
            log.exception("alert send failed")
            return False

    async def record_failure(self, key: str, message: str) -> bool:
        if self._send is None:
            return False
        state = self._load()
        if state.get(key) == "fail":
            return False
        sent = await self._post(message)
        state[key] = "fail"
        self._store(state)
        return sent

    async def record_success(self, key: str) -> bool:
        if self._send is None:
            return False
        state = self._load()
        if state.get(key) != "fail":
            return False
        sent = await self._post(f"recovered: `{key}` is healthy again")
        state[key] = "ok"
        self._store(state)
        return sent


def slack_send(webhook_url: str, session: aiohttp.ClientSession) -> SendFn:
    """Build a SendFn that posts `{"text": ...}` to a Slack incoming webhook."""

    async def _send(text: str) -> None:
        async with session.post(webhook_url, json={"text": text}) as resp:
            resp.raise_for_status()

    return _send
