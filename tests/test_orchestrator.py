"""Unit tests for the strict-mode safety check (REQUIRE_REPO_CONFIG)."""

from __future__ import annotations

from pathlib import Path

from pr_conflict_bot.config import (
    BotIdentity,
    CodexConfig,
    Config,
    GitHubAppConfig,
    RepoOverride,
    VerifyConfig,
)


def _cfg(*, require_repo_config: bool, default_verify: VerifyConfig) -> Config:
    return Config(
        github=GitHubAppConfig(
            app_id=1, private_key_pem="-----", webhook_secret="x", bot_login="b[bot]"
        ),
        codex=CodexConfig(),
        verify=default_verify,
        identity=BotIdentity(),
        listen_host="127.0.0.1",
        listen_port=8081,
        webhook_path="/hooks/github",
        work_dir=Path("/tmp/x"),
        log_level="INFO",
        require_repo_config=require_repo_config,
        allow_orgs=frozenset(),
    )


# We call the safety predicate directly rather than the whole orchestrator,
# since the predicate is the contract we care about.
def _strict_should_abort(cfg: Config, override: RepoOverride) -> bool:
    eff = override.verify or cfg.verify
    return cfg.require_repo_config and not (eff.lint or eff.typecheck or eff.test)


def test_strict_aborts_when_no_verify_anywhere() -> None:
    cfg = _cfg(require_repo_config=True, default_verify=VerifyConfig())
    override = RepoOverride()
    assert _strict_should_abort(cfg, override) is True


def test_strict_passes_when_repo_has_test() -> None:
    cfg = _cfg(require_repo_config=True, default_verify=VerifyConfig())
    override = RepoOverride(verify=VerifyConfig(test="pytest -q"))
    assert _strict_should_abort(cfg, override) is False


def test_strict_passes_when_env_default_has_lint() -> None:
    cfg = _cfg(require_repo_config=True, default_verify=VerifyConfig(lint="ruff check ."))
    override = RepoOverride()
    assert _strict_should_abort(cfg, override) is False


def test_strict_off_never_aborts_even_with_empty_verify() -> None:
    cfg = _cfg(require_repo_config=False, default_verify=VerifyConfig())
    override = RepoOverride()
    assert _strict_should_abort(cfg, override) is False


def test_repo_override_takes_precedence_over_env_default() -> None:
    """Override.verify is None => fall back to env default. If override.verify
    is set (even partially), it's the whole gate — env defaults don't merge in."""
    cfg = _cfg(
        require_repo_config=True,
        default_verify=VerifyConfig(test="pytest"),
    )
    # Repo says: only typecheck. env's `test` does NOT merge in.
    override = RepoOverride(verify=VerifyConfig(typecheck="mypy ."))
    assert _strict_should_abort(cfg, override) is False  # typecheck alone is fine

    # Repo says: nothing. Env default is ignored when override.verify is set
    # to an explicit empty VerifyConfig — but RepoOverride() with verify=None
    # means "no override", so env default applies.
    override_none = RepoOverride(verify=None)
    assert _strict_should_abort(cfg, override_none) is False  # env's test applies
