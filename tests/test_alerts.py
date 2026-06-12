"""Unit tests for failure categorization and edge-triggered ops alerts.

Contract under test:
- `categorize` maps resolution exceptions to a stable category so operators
  can tell an auth outage from a flaky LLM at a glance.
- `public_reason` is the one-liner that goes in the PR comment. It must NEVER
  contain raw CLI output (stdout/stderr can carry env material).
- `Alerter` fires only on the ok->fail transition (and recovery on fail->ok),
  never on every chronic failure, with state persisted across restarts.
"""

from __future__ import annotations

from pathlib import Path

from pr_conflict_bot.alerts import Alerter, categorize, public_reason
from pr_conflict_bot.llm import LLMError

# --- categorize -------------------------------------------------------------


def test_categorize_auth_401() -> None:
    e = LLMError(
        "claude failed (rc=1):\nstdout:\nFailed to authenticate. "
        "API Error: 401 Invalid authentication credentials\n\nstderr:\n"
    )
    assert categorize(e) == "auth failure"


def test_categorize_auth_not_logged_in() -> None:
    assert categorize(LLMError("Not logged in · Please run /login")) == "auth failure"


def test_categorize_timeout() -> None:
    assert categorize(LLMError("claude timed out after 300.0s")) == "timeout"


def test_categorize_generic_llm_error() -> None:
    assert categorize(LLMError("claude left conflict markers in foo.ts")) == "LLM error"


def test_categorize_non_llm_exception() -> None:
    assert categorize(ValueError("boom")) == "unexpected error"


# --- public_reason ----------------------------------------------------------


def test_public_reason_names_file_and_category() -> None:
    e = LLMError("claude failed (rc=1): 401 Invalid authentication credentials")
    reason = public_reason("src/foo.ts", e)
    assert "`src/foo.ts`" in reason
    assert "auth failure" in reason
    assert "\n" not in reason  # one line


def test_public_reason_never_leaks_exception_text() -> None:
    e = LLMError(
        "claude failed (rc=1):\nstdout:\nTOPSECRET_TOKEN_VALUE leaked here\nstderr:\n401"
    )
    reason = public_reason("src/foo.ts", e)
    assert "TOPSECRET_TOKEN_VALUE" not in reason
    assert "stdout" not in reason


# --- Alerter edge-triggering ------------------------------------------------


class _FakeSend:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


async def test_first_failure_sends_alert(tmp_path: Path) -> None:
    send = _FakeSend()
    a = Alerter(state_path=tmp_path / "state.json", send=send)
    sent = await a.record_failure("resolve", "auth failure on repo X")
    assert sent is True
    assert send.messages == ["auth failure on repo X"]


async def test_chronic_failure_does_not_resend(tmp_path: Path) -> None:
    send = _FakeSend()
    a = Alerter(state_path=tmp_path / "state.json", send=send)
    await a.record_failure("resolve", "first")
    sent = await a.record_failure("resolve", "second")
    assert sent is False
    assert send.messages == ["first"]


async def test_recovery_sends_once(tmp_path: Path) -> None:
    send = _FakeSend()
    a = Alerter(state_path=tmp_path / "state.json", send=send)
    await a.record_failure("resolve", "down")
    assert await a.record_success("resolve") is True
    assert await a.record_success("resolve") is False
    assert len(send.messages) == 2  # one failure + one recovery


async def test_success_when_already_ok_sends_nothing(tmp_path: Path) -> None:
    send = _FakeSend()
    a = Alerter(state_path=tmp_path / "state.json", send=send)
    assert await a.record_success("resolve") is False
    assert send.messages == []


async def test_state_persists_across_instances(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    send1 = _FakeSend()
    await Alerter(state_path=state, send=send1).record_failure("resolve", "down")
    # Simulate a bot restart mid-outage: new instance, same state file.
    send2 = _FakeSend()
    sent = await Alerter(state_path=state, send=send2).record_failure("resolve", "still down")
    assert sent is False
    assert send2.messages == []


async def test_no_send_configured_is_a_noop(tmp_path: Path) -> None:
    a = Alerter(state_path=tmp_path / "state.json", send=None)
    assert await a.record_failure("resolve", "down") is False
    assert await a.record_success("resolve") is False


async def test_keys_are_independent(tmp_path: Path) -> None:
    send = _FakeSend()
    a = Alerter(state_path=tmp_path / "state.json", send=send)
    await a.record_failure("resolve", "resolve down")
    sent = await a.record_failure("qa", "qa down")
    assert sent is True
    assert send.messages == ["resolve down", "qa down"]


# --- slack bot-token sender + send selection --------------------------------


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Captures post() calls; returns a configurable Slack-style body."""

    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[dict] = []
        self._payload = payload or {"ok": True}

    def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return _FakeResponse(self._payload)


async def test_slack_bot_send_posts_chat_postmessage(tmp_path: Path) -> None:
    from pr_conflict_bot.alerts import slack_bot_send

    session = _FakeSession()
    send = slack_bot_send("xoxb-test-token", "U123", session)  # type: ignore[arg-type]
    await send("hello ops")
    (call,) = session.calls
    assert call["url"] == "https://slack.com/api/chat.postMessage"
    assert call["headers"]["Authorization"] == "Bearer xoxb-test-token"
    assert call["json"] == {"channel": "U123", "text": "hello ops"}


async def test_slack_bot_send_raises_on_ok_false() -> None:
    """Slack returns HTTP 200 with ok=false on API errors — must not pass silently."""
    import pytest

    from pr_conflict_bot.alerts import slack_bot_send

    session = _FakeSession({"ok": False, "error": "channel_not_found"})
    send = slack_bot_send("xoxb-test-token", "U123", session)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="channel_not_found"):
        await send("hello")


def test_build_send_unconfigured_returns_none() -> None:
    from pr_conflict_bot.alerts import build_send

    assert build_send(None, None, None, _FakeSession()) is None  # type: ignore[arg-type]
    # token without channel (or vice versa) is misconfig -> disabled, not a crash
    assert build_send(None, "xoxb-x", None, _FakeSession()) is None  # type: ignore[arg-type]
    assert build_send(None, None, "U123", _FakeSession()) is None  # type: ignore[arg-type]


def test_build_send_prefers_webhook_then_bot_token() -> None:
    from pr_conflict_bot.alerts import build_send

    assert build_send("https://hooks.slack.com/x", None, None, _FakeSession()) is not None  # type: ignore[arg-type]
    assert build_send(None, "xoxb-x", "U123", _FakeSession()) is not None  # type: ignore[arg-type]
