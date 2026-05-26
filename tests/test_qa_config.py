from __future__ import annotations

from pathlib import Path

from pr_conflict_bot.config import QAConfig, load_repo_override


def test_qa_defaults_when_no_toml(tmp_path: Path) -> None:
    ov = load_repo_override(tmp_path)
    assert ov.qa == QAConfig()  # disabled, report, standard, ("functional",)
    assert ov.qa.enabled is False


def test_qa_block_parsed(tmp_path: Path) -> None:
    (tmp_path / ".pr-conflict-bot.toml").write_text(
        """
        [qa]
        enabled = true
        mode = "report"
        tier = "exhaustive"
        lens = ["functional", "design"]
        url = "http://localhost:3000"
        start = "npm run dev"
        build = "npm run build"
        """
    )
    ov = load_repo_override(tmp_path)
    assert ov.qa == QAConfig(
        enabled=True,
        mode="report",
        tier="exhaustive",
        lens=("functional", "design"),
        url="http://localhost:3000",
        start="npm run dev",
        build="npm run build",
    )


def test_qa_absent_block_is_disabled(tmp_path: Path) -> None:
    (tmp_path / ".pr-conflict-bot.toml").write_text('[verify]\ntest = "pytest"\n')
    ov = load_repo_override(tmp_path)
    assert ov.qa.enabled is False
