"""Infer how to build + serve a repo so org-wide auto-QA can run without a
per-repo `[qa] start`. Best-effort: returns None when nothing servable is
detected, which the orchestrator treats as "silently skip this repo".

Detection is deliberately conservative — a wrong guess just wastes a build and
then times out into a silent skip, so we only emit a spec when a known web
framework or an obvious dev/start script is present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_STATIC_PORT = 8080


@dataclass(frozen=True)
class ServeSpec:
    start: str
    url: str
    build: str  # "" when no separate build step is needed


def _package_manager(repo_dir: Path) -> str:
    if (repo_dir / "bun.lockb").exists() or (repo_dir / "bun.lock").exists():
        return "bun"
    if (repo_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_dir / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _install_cmd(pm: str) -> str:
    # npm gets `ci` (clean, lockfile-faithful); others use their install verb.
    return "npm ci" if pm == "npm" else f"{pm} install"


def detect_serve(repo_dir: Path) -> ServeSpec | None:
    pkg_path = repo_dir / "package.json"
    if pkg_path.is_file():
        try:
            pkg = json.loads(pkg_path.read_text())
        except (json.JSONDecodeError, OSError):
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {}) or {}
        pm = _package_manager(repo_dir)
        install = _install_cmd(pm)
        run = "npm run" if pm == "npm" else f"{pm} run"
        start_verb = "npm start" if pm == "npm" else f"{pm} start"

        if "next" in deps:
            return ServeSpec(
                start=f"{run} start", build=f"{install} && {run} build",
                url="http://localhost:3000",
            )
        if "vite" in deps:
            return ServeSpec(
                start=f"{run} preview", build=f"{install} && {run} build",
                url="http://localhost:4173",
            )
        if "react-scripts" in deps:
            return ServeSpec(
                start=f"{install} && {start_verb}", build="", url="http://localhost:3000"
            )
        if "dev" in scripts:
            return ServeSpec(
                start=f"{install} && {run} dev", build="", url="http://localhost:3000"
            )
        if "start" in scripts:
            return ServeSpec(
                start=f"{install} && {start_verb}", build="", url="http://localhost:3000"
            )
        return None

    if (repo_dir / "index.html").is_file():
        return ServeSpec(
            start=f"python3 -m http.server {_STATIC_PORT}",
            build="",
            url=f"http://localhost:{_STATIC_PORT}",
        )
    return None
