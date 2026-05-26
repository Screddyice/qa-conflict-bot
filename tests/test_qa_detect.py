from __future__ import annotations

import json
from pathlib import Path

from pr_conflict_bot.qa.detect import detect_serve


def _pkg(tmp_path: Path, deps: dict[str, str], scripts: dict[str, str] | None = None) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": deps, "scripts": scripts or {}})
    )


def test_next_app(tmp_path: Path) -> None:
    _pkg(tmp_path, {"next": "14"}, {"build": "next build", "start": "next start"})
    spec = detect_serve(tmp_path)
    assert spec is not None
    assert "build" in spec.build  # needs a build before start
    assert "start" in spec.start
    assert spec.url == "http://localhost:3000"


def test_vite_app_uses_preview_4173(tmp_path: Path) -> None:
    _pkg(tmp_path, {"vite": "5"}, {"build": "vite build", "preview": "vite preview"})
    spec = detect_serve(tmp_path)
    assert spec is not None
    assert "preview" in spec.start
    assert spec.url == "http://localhost:4173"


def test_react_scripts_no_build(tmp_path: Path) -> None:
    _pkg(tmp_path, {"react-scripts": "5"}, {"start": "react-scripts start"})
    spec = detect_serve(tmp_path)
    assert spec is not None
    assert "start" in spec.start
    assert spec.build == ""  # CRA dev server needs no separate build
    assert spec.url == "http://localhost:3000"


def test_generic_dev_script(tmp_path: Path) -> None:
    _pkg(tmp_path, {"express": "4"}, {"dev": "node server.js"})
    spec = detect_serve(tmp_path)
    assert spec is not None
    assert "run dev" in spec.start


def test_package_manager_from_lockfile(tmp_path: Path) -> None:
    _pkg(tmp_path, {"next": "14"}, {"build": "next build", "start": "next start"})
    (tmp_path / "yarn.lock").write_text("")
    spec = detect_serve(tmp_path)
    assert spec is not None
    assert spec.start.startswith("yarn")
    assert "yarn" in spec.build


def test_static_site_no_package_json(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    spec = detect_serve(tmp_path)
    assert spec is not None
    assert "http.server" in spec.start
    assert spec.url.startswith("http://localhost:")


def test_undetectable_returns_none(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('backend, no web')")
    assert detect_serve(tmp_path) is None


def test_package_json_without_servable_script_returns_none(tmp_path: Path) -> None:
    _pkg(tmp_path, {"lodash": "4"}, {"test": "jest"})
    assert detect_serve(tmp_path) is None
