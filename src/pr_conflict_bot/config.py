"""Configuration loaded from environment variables and per-repo TOML overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubAppConfig:
    app_id: int
    private_key_pem: str
    webhook_secret: str
    bot_login: str  # e.g. "pr-conflict-bot[bot]" — used to filter self-triggered events


@dataclass(frozen=True)
class CodexConfig:
    binary: str = "codex"
    sandbox: str = "workspace-write"
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerifyConfig:
    """Commands to run as the verify gate. Empty string disables that step."""
    lint: str = ""
    typecheck: str = ""
    test: str = ""
    timeout_seconds: int = 600


@dataclass(frozen=True)
class BotIdentity:
    git_name: str = "pr-conflict-bot"
    git_email: str = "pr-conflict-bot@users.noreply.github.com"


@dataclass(frozen=True)
class Config:
    github: GitHubAppConfig
    codex: CodexConfig
    verify: VerifyConfig
    identity: BotIdentity
    listen_host: str
    listen_port: int
    work_dir: Path
    log_level: str
    allow_orgs: frozenset[str] = field(default_factory=frozenset)


def _required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise ConfigError(f"Required environment variable {key} is not set")
    return val


def _read_private_key(path_or_inline: str) -> str:
    if path_or_inline.lstrip().startswith("-----BEGIN"):
        return path_or_inline
    p = Path(path_or_inline).expanduser()
    if not p.is_file():
        raise ConfigError(f"GITHUB_APP_PRIVATE_KEY does not point to a file: {p}")
    return p.read_text()


def load_from_env() -> Config:
    return Config(
        github=GitHubAppConfig(
            app_id=int(_required("GITHUB_APP_ID")),
            private_key_pem=_read_private_key(_required("GITHUB_APP_PRIVATE_KEY")),
            webhook_secret=_required("GITHUB_WEBHOOK_SECRET"),
            bot_login=_required("GITHUB_BOT_LOGIN"),
        ),
        codex=CodexConfig(
            binary=os.environ.get("CODEX_BIN", "codex"),
            sandbox=os.environ.get("CODEX_SANDBOX", "workspace-write"),
            extra_args=tuple(
                a for a in os.environ.get("CODEX_EXTRA_ARGS", "").split() if a
            ),
        ),
        verify=VerifyConfig(
            lint=os.environ.get("VERIFY_LINT", ""),
            typecheck=os.environ.get("VERIFY_TYPECHECK", ""),
            test=os.environ.get("VERIFY_TEST", ""),
            timeout_seconds=int(os.environ.get("VERIFY_TIMEOUT", "600")),
        ),
        identity=BotIdentity(
            git_name=os.environ.get("BOT_GIT_NAME", "pr-conflict-bot"),
            git_email=os.environ.get(
                "BOT_GIT_EMAIL", "pr-conflict-bot@users.noreply.github.com"
            ),
        ),
        listen_host=os.environ.get("LISTEN_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("LISTEN_PORT", "8081")),
        work_dir=Path(os.environ.get("WORK_DIR", "/var/lib/pr-conflict-bot/work")).expanduser(),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        allow_orgs=frozenset(
            o.strip().lower()
            for o in os.environ.get("ALLOW_ORGS", "").split(",")
            if o.strip()
        ),
    )


@dataclass(frozen=True)
class RepoOverride:
    """Per-repo overrides loaded from .pr-conflict-bot.toml in the target repo."""
    verify: VerifyConfig | None = None
    skip_paths: tuple[str, ...] = ()
    max_files_per_pr: int = 50
    enabled: bool = True


def load_repo_override(repo_root: Path) -> RepoOverride:
    cfg_path = repo_root / ".pr-conflict-bot.toml"
    if not cfg_path.is_file():
        return RepoOverride()
    data = tomllib.loads(cfg_path.read_text())
    v = data.get("verify", {})
    verify = VerifyConfig(
        lint=v.get("lint", ""),
        typecheck=v.get("typecheck", ""),
        test=v.get("test", ""),
        timeout_seconds=int(v.get("timeout_seconds", 600)),
    ) if v else None
    behavior = data.get("behavior", {})
    return RepoOverride(
        verify=verify,
        skip_paths=tuple(behavior.get("skip_paths", [])),
        max_files_per_pr=int(behavior.get("max_files_per_pr", 50)),
        enabled=bool(behavior.get("enabled", True)),
    )
