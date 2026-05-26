from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from pr_conflict_bot.config import (
    BotIdentity,
    Config,
    GitHubAppConfig,
    LLMConfig,
    QAConfig,
    VerifyConfig,
)
from pr_conflict_bot.qa.browse import FakeBrowse, PageState
from pr_conflict_bot.qa.orchestrator import QADeps, process_qa_job
from pr_conflict_bot.server import PRJob


class FakeGH:
    def __init__(self) -> None:
        self.comments: list[str] = []

    async def clone_url(self, installation_id: int, owner: str, repo: str) -> str:
        return "file:///unused"

    async def post_issue_comment(
        self, installation_id: int, owner: str, repo: str, pr_number: int, body: str
    ) -> None:
        self.comments.append(body)


def _cfg() -> Config:
    return Config(
        github=GitHubAppConfig(app_id=1, private_key_pem="x", webhook_secret="s", bot_login="b[bot]"),
        llm=LLMConfig(backend="claude"),
        verify=VerifyConfig(),
        identity=BotIdentity(),
        listen_host="127.0.0.1",
        listen_port=8081,
        webhook_path="/hooks/github",
        work_dir=Path("/tmp"),
        log_level="INFO",
    )


def _job() -> PRJob:
    return PRJob(
        delivery_id="d", installation_id=1, owner="o", repo="r", pr_number=7,
        pr_branch="feat", base_branch="main", pr_head_sha="abc", sender_login="u", sender_type="User",
    )


def _deps(repo_dir: Path, *, qa: QAConfig, browse: FakeBrowse, completion: str) -> QADeps:
    @contextlib.asynccontextmanager
    async def fake_open_url(_repo: Path, _qa: QAConfig) -> AsyncIterator[str]:
        yield "http://app.test"

    async def fake_complete(prompt: str, cfg: LLMConfig) -> str:
        return completion

    async def fake_clone(_job: PRJob, _cfg: Config, _gh: object) -> Path:
        return repo_dir

    async def fake_cleanup(_repo: Path) -> None:
        return None

    return QADeps(
        load_qa=lambda _root: qa,
        clone=fake_clone,
        open_url=fake_open_url,
        browse=browse,
        complete=fake_complete,
        cleanup=fake_cleanup,
    )


async def test_disabled_repo_posts_no_comment(tmp_path: Path) -> None:
    gh = FakeGH()
    deps = _deps(
        tmp_path,
        qa=QAConfig(enabled=False),
        browse=FakeBrowse(PageState("http://app.test", 200, (), "ok", None)),
        completion="[]",
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert gh.comments == []


async def test_clean_page_posts_no_issues_comment(tmp_path: Path) -> None:
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 200, (), "Welcome", None))
    deps = _deps(tmp_path, qa=QAConfig(enabled=True, url="http://app.test", start="x"), browse=browse, completion="[]")
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1
    assert "No issues found" in gh.comments[0]
    assert browse.captured_urls == ["http://app.test"]


async def test_findings_appear_in_comment(tmp_path: Path) -> None:
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 500, ("TypeError",), "Error", None))
    completion = '[{"severity":"high","title":"500","detail":"server error"}]'
    deps = _deps(tmp_path, qa=QAConfig(enabled=True, url="http://app.test", start="x"), browse=browse, completion=completion)
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert "500" in gh.comments[0]
    assert "/10" in gh.comments[0]
