"""Webhook signature verification + skip-reason logic. No network."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from pr_conflict_bot.config import (
    BotIdentity,
    CodexConfig,
    Config,
    GitHubAppConfig,
    VerifyConfig,
)
from pr_conflict_bot.server import _should_handle, _verify_signature


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_matches() -> None:
    secret = "supersekret"
    body = b'{"action":"opened"}'
    assert _verify_signature(secret, body, _sign(secret, body))


def test_signature_rejects_wrong_secret() -> None:
    body = b'{"action":"opened"}'
    assert not _verify_signature("right", body, _sign("wrong", body))


def test_signature_rejects_missing_header() -> None:
    assert not _verify_signature("s", b"x", None)
    assert not _verify_signature("s", b"x", "md5=abc")


@pytest.fixture
def cfg() -> Config:
    return Config(
        github=GitHubAppConfig(
            app_id=1,
            private_key_pem="-----BEGIN-----",
            webhook_secret="x",
            bot_login="pr-conflict-bot[bot]",
        ),
        codex=CodexConfig(),
        verify=VerifyConfig(),
        identity=BotIdentity(),
        listen_host="127.0.0.1",
        listen_port=8081,
        webhook_path="/hooks/github",
        work_dir=__import__("pathlib").Path("/tmp/x"),
        log_level="INFO",
        allow_orgs=frozenset({"acme"}),
    )


def _payload(**overrides: object) -> dict:
    base = {
        "action": "opened",
        "pull_request": {"draft": False, "state": "open"},
        "repository": {"owner": {"login": "acme"}},
    }
    base.update(overrides)
    return base


def test_skip_unhandled_action(cfg: Config) -> None:
    p = _payload(action="closed")
    assert _should_handle(p, cfg, "alice", "User") == "action=closed ignored"


def test_skip_draft_pr(cfg: Config) -> None:
    p = _payload(pull_request={"draft": True, "state": "open"})
    assert _should_handle(p, cfg, "alice", "User") == "PR is draft"


def test_skip_org_not_in_allowlist(cfg: Config) -> None:
    p = _payload(repository={"owner": {"login": "other"}})
    reason = _should_handle(p, cfg, "alice", "User")
    assert reason is not None and "not in ALLOW_ORGS" in reason


def test_skip_self_triggered(cfg: Config) -> None:
    p = _payload()
    reason = _should_handle(p, cfg, "pr-conflict-bot[bot]", "Bot")
    assert reason == "self-triggered (bot push)"


def test_handle_normal_pr(cfg: Config) -> None:
    p = _payload()
    assert _should_handle(p, cfg, "alice", "User") is None
