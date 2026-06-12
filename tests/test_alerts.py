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
