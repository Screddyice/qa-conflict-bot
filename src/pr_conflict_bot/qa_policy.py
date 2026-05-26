"""Resolve the effective QA config for a repo, applying org defaults + RS21 block.

QA enablement is tri-state: a repo's `.pr-conflict-bot.toml` can explicitly set
`[qa] enabled`/`mode` (wins), or stay silent and inherit the org default
(`QA_DEFAULT_ENABLED_ORGS` / `QA_DEFAULT_MODE`). RS21 repos are hard-excluded
regardless — the workspace lockdown forbids any automation on them.
"""

from __future__ import annotations

from dataclasses import replace

from .config import Config, QAConfig, RepoOverride


def is_rs21(owner: str, repo: str) -> bool:
    """True if this repo is under the RS21 no-automation lockdown.

    Matched by 'rs21' appearing in the owner or repo name (case-insensitive),
    mirroring the workspace rule that RS21 repos get NO code-landing automation.
    """
    return "rs21" in owner.lower() or "rs21" in repo.lower()


def resolve_qa(override: RepoOverride, cfg: Config, owner: str, repo: str) -> QAConfig:
    """Effective QAConfig after precedence: RS21 force-off > repo TOML > org default.

    - RS21: always disabled, mode forced to "report" (belt-and-suspenders; nothing
      should run, but if a caller ignores `enabled` it still won't fix-mode).
    - enabled: repo's explicit value if it set one, else owner ∈ org default set.
    - mode: repo's explicit value if it set one, else the org default mode.
    """
    if is_rs21(owner, repo):
        return replace(override.qa, enabled=False, mode="report")

    enabled = override.qa.enabled if override.qa_enabled_set else (
        owner.lower() in cfg.qa_default_enabled_orgs
    )
    mode = override.qa.mode if override.qa_mode_set else cfg.qa_default_mode
    return replace(override.qa, enabled=enabled, mode=mode)
