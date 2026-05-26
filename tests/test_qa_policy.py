from __future__ import annotations

from pathlib import Path

from pr_conflict_bot.config import (
    BotIdentity,
    Config,
    GitHubAppConfig,
    LLMConfig,
    QAConfig,
    RepoOverride,
    VerifyConfig,
)
from pr_conflict_bot.qa_policy import is_rs21, resolve_qa


def _cfg(*, default_orgs: frozenset[str] = frozenset(), default_mode: str = "report") -> Config:
    return Config(
        github=GitHubAppConfig(app_id=1, private_key_pem="x", webhook_secret="s", bot_login="b[bot]"),
        llm=LLMConfig(), verify=VerifyConfig(), identity=BotIdentity(),
        listen_host="127.0.0.1", listen_port=8081, webhook_path="/hooks/github",
        work_dir=Path("/tmp"), log_level="INFO",
        qa_default_enabled_orgs=default_orgs, qa_default_mode=default_mode,
    )


def test_is_rs21_matches_name_case_insensitive() -> None:
    assert is_rs21("teamnebula-ai", "rs21-jira-mirror")
    assert is_rs21("teamnebula-ai", "RS21-Thing")
    assert is_rs21("someone", "client-rs21-portal")
    assert not is_rs21("teamnebula-ai", "hyperscale")


def test_rs21_forced_off_even_when_enabled() -> None:
    # A repo TOML that explicitly enables QA is still hard-blocked for RS21.
    ov = RepoOverride(qa=QAConfig(enabled=True, mode="fix"), qa_enabled_set=True, qa_mode_set=True)
    eff = resolve_qa(ov, _cfg(default_orgs=frozenset({"teamnebula-ai"})), "teamnebula-ai", "rs21-mirror")
    assert eff.enabled is False
    assert eff.mode == "report"


def test_org_default_enables_when_toml_silent() -> None:
    ov = RepoOverride()  # no [qa] block at all
    eff = resolve_qa(
        ov, _cfg(default_orgs=frozenset({"teamnebula-ai", "the-ready-consult"}), default_mode="fix"),
        "TeamNebula-AI", "hyperscale",
    )
    assert eff.enabled is True
    assert eff.mode == "fix"  # inherits the org default mode


def test_repo_optout_beats_org_default() -> None:
    ov = RepoOverride(qa=QAConfig(enabled=False), qa_enabled_set=True)
    eff = resolve_qa(ov, _cfg(default_orgs=frozenset({"teamnebula-ai"})), "teamnebula-ai", "hyperscale")
    assert eff.enabled is False


def test_explicit_mode_beats_org_default_mode() -> None:
    ov = RepoOverride(qa=QAConfig(enabled=True, mode="report"), qa_enabled_set=True, qa_mode_set=True)
    eff = resolve_qa(ov, _cfg(default_orgs=frozenset({"teamnebula-ai"}), default_mode="fix"), "teamnebula-ai", "x")
    assert eff.mode == "report"


def test_owner_not_in_default_orgs_stays_disabled() -> None:
    ov = RepoOverride()
    eff = resolve_qa(ov, _cfg(default_orgs=frozenset({"teamnebula-ai"})), "screddyice", "some-repo")
    assert eff.enabled is False
