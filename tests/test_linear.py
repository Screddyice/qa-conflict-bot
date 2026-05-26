from __future__ import annotations

import json
from typing import Any

import pytest

from pr_conflict_bot.config import _parse_linear_tokens
from pr_conflict_bot.linear import LinearClient, LinearError


class _FakeResp:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> _FakeResp:
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    async def json(self) -> dict[str, Any]:
        return self._payload

    async def text(self) -> str:
        return json.dumps(self._payload)


class _FakeSession:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self._status = status
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> _FakeResp:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResp(self._status, self._payload)


# --- config: LINEAR_TOKENS parsing ------------------------------------------


def test_parse_linear_tokens_lowercases_owners() -> None:
    out = _parse_linear_tokens('{"TeamNebula-AI": "lin_a", "The-Ready-Consult": "lin_b"}')
    assert out == {"teamnebula-ai": "lin_a", "the-ready-consult": "lin_b"}


def test_parse_linear_tokens_empty_and_malformed_are_empty() -> None:
    assert _parse_linear_tokens("") == {}
    assert _parse_linear_tokens("   ") == {}
    assert _parse_linear_tokens("not json") == {}
    assert _parse_linear_tokens('["a","b"]') == {}  # not an object
    assert _parse_linear_tokens('{"o": ""}') == {}  # empty token dropped


# --- LinearClient -----------------------------------------------------------


async def test_find_issue_id_parses_attachment_and_sends_auth() -> None:
    session = _FakeSession(
        {"data": {"attachmentsForURL": {"nodes": [{"issue": {"id": "issue-123"}}]}}}
    )
    client = LinearClient("lin_tok", session)  # type: ignore[arg-type]
    issue_id = await client.find_issue_id_by_url("https://github.com/o/r/pull/5")

    assert issue_id == "issue-123"
    call = session.calls[0]
    assert call["url"] == "https://api.linear.app/graphql"
    # Linear personal keys: raw token in Authorization, no "Bearer".
    assert call["headers"]["Authorization"] == "lin_tok"
    assert "attachmentsForURL" in call["json"]["query"]
    assert call["json"]["variables"] == {"url": "https://github.com/o/r/pull/5"}


async def test_find_issue_id_returns_none_when_no_attachment() -> None:
    session = _FakeSession({"data": {"attachmentsForURL": {"nodes": []}}})
    client = LinearClient("t", session)  # type: ignore[arg-type]
    assert await client.find_issue_id_by_url("https://github.com/o/r/pull/9") is None


async def test_comment_succeeds() -> None:
    session = _FakeSession({"data": {"commentCreate": {"success": True}}})
    client = LinearClient("t", session)  # type: ignore[arg-type]
    await client.comment("issue-123", "hello")
    call = session.calls[0]
    assert call["json"]["variables"] == {"issueId": "issue-123", "body": "hello"}
    assert "commentCreate" in call["json"]["query"]


async def test_comment_raises_when_not_successful() -> None:
    session = _FakeSession({"data": {"commentCreate": {"success": False}}})
    client = LinearClient("t", session)  # type: ignore[arg-type]
    with pytest.raises(LinearError):
        await client.comment("issue-123", "hello")


async def test_graphql_errors_raise() -> None:
    session = _FakeSession({"errors": [{"message": "bad"}]})
    client = LinearClient("t", session)  # type: ignore[arg-type]
    with pytest.raises(LinearError):
        await client.find_issue_id_by_url("https://github.com/o/r/pull/1")


async def test_graphql_http_failure_raises() -> None:
    session = _FakeSession({}, status=401)
    client = LinearClient("t", session)  # type: ignore[arg-type]
    with pytest.raises(LinearError):
        await client.comment("i", "b")
