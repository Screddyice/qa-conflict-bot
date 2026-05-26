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
class LLMConfig:
    """LLM backend used to resolve a single conflicted file.

    Two backends supported. Both are billed-as-subscription, not per-token API.
    - "claude": Claude Code CLI in headless mode (`claude -p`). Auth via
      CLAUDE_CODE_OAUTH_TOKEN (generated on a desktop with `claude setup-token`).
      Charges against your Claude Pro/Max subscription.
    - "codex": OpenAI Codex CLI (`codex exec`). Auth via `codex login` on
      the host. Charges against your ChatGPT Plus/Pro subscription.
    """
    backend: str = "claude"
    binary: str = ""  # empty → default ("claude" or "codex")
    extra_args: tuple[str, ...] = ()
    # claude backend only
    oauth_token: str = ""
    # codex backend only
    sandbox: str = "workspace-write"

    def resolved_binary(self) -> str:
        if self.binary:
            return self.binary
        return "claude" if self.backend == "claude" else "codex"


@dataclass(frozen=True)
class VerifyConfig:
    """Commands to run as the verify gate. Empty string disables that step."""
    lint: str = ""
    typecheck: str = ""
    test: str = ""
    timeout_seconds: int = 600


@dataclass(frozen=True)
class QAConfig:
    """Per-repo QA-mode settings from the `[qa]` table of .pr-conflict-bot.toml.

    QA is opt-in: `enabled` defaults to False so no repo runs QA until it asks.
    `mode = "fix"` is reserved for M4 and hard-blocked on RS21 repos there.
    """
    enabled: bool = False
    mode: str = "report"            # "report" | "fix"
    tier: str = "standard"          # "quick" | "standard" | "exhaustive"
    lens: tuple[str, ...] = ("functional",)
    url: str = ""                   # start-command fallback target
    start: str = ""                 # command to serve the app
    build: str = ""                 # optional pre-start build command


@dataclass(frozen=True)
class BotIdentity:
    git_name: str = "pr-conflict-bot"
    git_email: str = "pr-conflict-bot@users.noreply.github.com"


@dataclass(frozen=True)
class Config:
    github: GitHubAppConfig
    llm: LLMConfig
    verify: VerifyConfig
    identity: BotIdentity
    listen_host: str
    listen_port: int
    webhook_path: str  # e.g. "/hooks/github" or "/prbot/hooks/github" for path-based routing
    work_dir: Path
    log_level: str
    require_repo_config: bool = False
    """If True, abort with a comment when the effective verify gate has no non-empty
    step (no lint, no typecheck, no test). This forces every repo to either ship a
    `.pr-conflict-bot.toml` with verify commands or opt out via `enabled = false`,
    so the bot can never push an unverified resolution."""
    default_skip_paths: tuple[str, ...] = ()
    """Glob patterns applied as the default `skip_paths` when a repo has no
    `.pr-conflict-bot.toml`. Lets you ship org-wide lockfile/migration skips
    without dropping a TOML in every repo."""
    default_max_files_per_pr: int = 50
    """Default `max_files_per_pr` when a repo has no `.pr-conflict-bot.toml`."""
    allow_orgs: frozenset[str] = field(default_factory=frozenset)
    allow_users: frozenset[str] = field(default_factory=frozenset)
    """If non-empty, only PRs whose author's GitHub login is in this set
    will be handled. Comparison is case-insensitive (logins normalized to
    lower-case at load time). Empty set means "no user filter" — all PRs
    pass this gate. Useful when the App is installed org-wide but should
    only act on a specific operator's PRs."""


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
        llm=LLMConfig(
            backend=os.environ.get("LLM_BACKEND", "claude").lower(),
            binary=os.environ.get("LLM_BIN", ""),
            extra_args=tuple(
                a for a in os.environ.get("LLM_EXTRA_ARGS", "").split() if a
            ),
            oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            sandbox=os.environ.get("CODEX_SANDBOX", "workspace-write"),
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
        webhook_path=os.environ.get("WEBHOOK_PATH", "/hooks/github"),
        work_dir=Path(os.environ.get("WORK_DIR", "/var/lib/pr-conflict-bot/work")).expanduser(),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        require_repo_config=os.environ.get("REQUIRE_REPO_CONFIG", "false").lower()
        in ("1", "true", "yes"),
        default_skip_paths=tuple(
            p.strip() for p in os.environ.get("DEFAULT_SKIP_PATHS", "").split(",") if p.strip()
        ),
        default_max_files_per_pr=int(os.environ.get("DEFAULT_MAX_FILES_PER_PR", "50")),
        allow_orgs=frozenset(
            o.strip().lower()
            for o in os.environ.get("ALLOW_ORGS", "").split(",")
            if o.strip()
        ),
        allow_users=frozenset(
            u.strip().lower()
            for u in os.environ.get("ALLOW_USERS", "").split(",")
            if u.strip()
        ),
    )


@dataclass(frozen=True)
class RepoOverride:
    """Per-repo overrides loaded from .pr-conflict-bot.toml in the target repo."""
    verify: VerifyConfig | None = None
    skip_paths: tuple[str, ...] = ()
    max_files_per_pr: int = 50
    enabled: bool = True
    qa: QAConfig = field(default_factory=QAConfig)


def load_repo_override(
    repo_root: Path,
    *,
    default_skip_paths: tuple[str, ...] = (),
    default_max_files_per_pr: int = 50,
) -> RepoOverride:
    """Load `.pr-conflict-bot.toml` if present, falling back to env-level defaults.

    The defaults let the operator ship org-wide skip_paths / max_files without
    needing a TOML in every repo.
    """
    cfg_path = repo_root / ".pr-conflict-bot.toml"
    if not cfg_path.is_file():
        return RepoOverride(
            skip_paths=default_skip_paths,
            max_files_per_pr=default_max_files_per_pr,
        )
    data = tomllib.loads(cfg_path.read_text())
    v = data.get("verify", {})
    verify = VerifyConfig(
        lint=v.get("lint", ""),
        typecheck=v.get("typecheck", ""),
        test=v.get("test", ""),
        timeout_seconds=int(v.get("timeout_seconds", 600)),
    ) if v else None
    behavior = data.get("behavior", {})
    q = data.get("qa", {})
    qa = QAConfig(
        enabled=bool(q.get("enabled", False)),
        mode=str(q.get("mode", "report")),
        tier=str(q.get("tier", "standard")),
        lens=tuple(q.get("lens", ["functional"])),
        url=str(q.get("url", "")),
        start=str(q.get("start", "")),
        build=str(q.get("build", "")),
    )
    # Repo TOML wins; env-defaults fill in only what's missing.
    skip_paths = behavior.get("skip_paths")
    max_files = behavior.get("max_files_per_pr")
    return RepoOverride(
        verify=verify,
        skip_paths=tuple(skip_paths) if skip_paths is not None else default_skip_paths,
        max_files_per_pr=int(max_files) if max_files is not None else default_max_files_per_pr,
        enabled=bool(behavior.get("enabled", True)),
        qa=qa,
    )
