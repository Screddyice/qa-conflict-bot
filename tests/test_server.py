"""Webhook signature verification + skip-reason logic. No network."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from pr_conflict_bot.config import (
    BotIdentity,
    Config,
    GitHubAppConfig,
    LLMConfig,
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
        llm=LLMConfig(),
        verify=VerifyConfig(),
        identity=BotIdentity(),
        listen_host="127.0.0.1",
        listen_port=8081,
        webhook_path="/hooks/github",
        work_dir=__import__("pathlib").Path("/tmp/x"),
        log_level="INFO",
        require_repo_config=False,
        default_skip_paths=(),
        default_max_files_per_pr=50,
        allow_orgs=frozenset({"acme"}),
    )


def _payload(**overrides: object) -> dict:
    base = {
        "action": "opened",
        "pull_request": {"draft": False, "state": "open", "user": {"login": "alice"}},
        "repository": {"owner": {"login": "acme"}},
    }
    base.update(overrides)
    return base


def test_skip_unhandled_action(cfg: Config) -> None:
    p = _payload(action="closed")
    assert _should_handle(p, cfg, "alice", "User") == "action=closed ignored"


def test_skip_draft_pr(cfg: Config) -> None:
    p = _payload(pull_request={"draft": True, "state": "open", "user": {"login": "alice"}})
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


def _cfg_with(*, allow_orgs: frozenset[str], allow_users: frozenset[str] = frozenset()) -> Config:
    return Config(
        github=GitHubAppConfig(
            app_id=1,
            private_key_pem="-----BEGIN-----",
            webhook_secret="x",
            bot_login="pr-conflict-bot[bot]",
        ),
        llm=LLMConfig(),
        verify=VerifyConfig(),
        identity=BotIdentity(),
        listen_host="127.0.0.1",
        listen_port=8081,
        webhook_path="/hooks/github",
        work_dir=__import__("pathlib").Path("/tmp/x"),
        log_level="INFO",
        require_repo_config=False,
        default_skip_paths=(),
        default_max_files_per_pr=50,
        allow_orgs=allow_orgs,
        allow_users=allow_users,
    )


def test_skip_pr_author_not_in_allowlist() -> None:
    # ALLOW_USERS set; PR author not in it → skip.
    cfg_user_locked = _cfg_with(allow_orgs=frozenset({"acme"}), allow_users=frozenset({"screddyice"}))
    p = _payload(pull_request={"draft": False, "state": "open", "user": {"login": "bob"}})
    reason = _should_handle(p, cfg_user_locked, "bob", "User")
    assert reason is not None and "not in ALLOW_USERS" in reason


def test_handle_pr_author_in_allowlist_case_insensitive() -> None:
    # ALLOW_USERS comparison is case-insensitive (load-time normalization
    # lowercases the env, runtime check lowercases the payload).
    cfg_user_locked = _cfg_with(allow_orgs=frozenset({"acme"}), allow_users=frozenset({"screddyice"}))
    for login in ("Screddyice", "screddyice", "SCREDDYICE"):
        p = _payload(pull_request={"draft": False, "state": "open", "user": {"login": login}})
        assert _should_handle(p, cfg_user_locked, login, "User") is None, login


def test_empty_allow_users_passes_any_author() -> None:
    # No ALLOW_USERS set → user gate is bypassed, any author passes.
    cfg_no_user_lock = _cfg_with(allow_orgs=frozenset({"acme"}))
    p = _payload(pull_request={"draft": False, "state": "open", "user": {"login": "stranger"}})
    assert _should_handle(p, cfg_no_user_lock, "stranger", "User") is None


def test_skip_cliqk_org_even_if_allowlisted() -> None:
    # The Cliqk org is hands-off; denylist must beat allowlist.
    cfg_with_cliqk = Config(
        github=GitHubAppConfig(
            app_id=1,
            private_key_pem="-----BEGIN-----",
            webhook_secret="x",
            bot_login="pr-conflict-bot[bot]",
        ),
        llm=LLMConfig(),
        verify=VerifyConfig(),
        identity=BotIdentity(),
        listen_host="127.0.0.1",
        listen_port=8081,
        webhook_path="/hooks/github",
        work_dir=__import__("pathlib").Path("/tmp/x"),
        log_level="INFO",
        require_repo_config=False,
        default_skip_paths=(),
        default_max_files_per_pr=50,
        allow_orgs=frozenset({"mycliqk", "acme"}),
    )
    for org in ("mycliqk", "MyCliqk", "cliqk"):
        p = _payload(repository={"owner": {"login": org}})
        reason = _should_handle(p, cfg_with_cliqk, "alice", "User")
        assert reason is not None and "DENY_ORGS" in reason, (org, reason)
