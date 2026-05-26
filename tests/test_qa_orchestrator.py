from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
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
from pr_conflict_bot.qa.browse import FakeBrowse, PageState
from pr_conflict_bot.qa.orchestrator import QADeps, process_qa_job
from pr_conflict_bot.qa.url_resolver import URLResolutionError
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


def _cfg(*, default_orgs: frozenset[str] = frozenset(), default_mode: str = "report") -> Config:
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
        qa_default_enabled_orgs=default_orgs,
        qa_default_mode=default_mode,
    )


def _job(owner: str = "o", repo: str = "r") -> PRJob:
    return PRJob(
        delivery_id="d", installation_id=1, owner=owner, repo=repo, pr_number=7,
        pr_branch="feat", base_branch="main", pr_head_sha="abc", sender_login="u", sender_type="User",
    )


def _deps(
    repo_dir: Path,
    *,
    qa: QAConfig,
    browse: FakeBrowse,
    completion: str,
    linear_calls: list[tuple[str, str, str]] | None = None,
    enabled_set: bool = True,
) -> QADeps:
    @contextlib.asynccontextmanager
    async def fake_open_url(_repo: Path, _qa: QAConfig) -> AsyncIterator[str]:
        yield "http://app.test"

    async def fake_complete(prompt: str, cfg: LLMConfig) -> str:
        return completion

    async def fake_clone(_job: PRJob, _cfg: Config, _gh: object) -> Path:
        return repo_dir

    async def fake_cleanup(_repo: Path) -> None:
        return None

    async def fake_notify_linear(owner: str, pr_url: str, body: str) -> bool:
        if linear_calls is not None:
            linear_calls.append((owner, pr_url, body))
        return True

    return QADeps(
        load_qa=lambda _root: RepoOverride(qa=qa, qa_enabled_set=enabled_set),
        clone=fake_clone,
        open_url=fake_open_url,
        browse=browse,
        complete=fake_complete,
        cleanup=fake_cleanup,
        notify_linear=fake_notify_linear,
    )


def _next_pkg(d: Path) -> None:
    (d / "package.json").write_text(
        json.dumps({"dependencies": {"next": "14"}, "scripts": {"build": "next build", "start": "next start"}})
    )


# --- explicit-repo behavior (unchanged contract) ----------------------------


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
    linear_calls: list[tuple[str, str, str]] = []
    deps = _deps(
        tmp_path,
        qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=browse,
        completion="[]",
        linear_calls=linear_calls,
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1
    assert "No issues found" in gh.comments[0]
    assert browse.captured_urls == ["http://app.test"]
    assert linear_calls == []


async def test_findings_appear_in_comment_and_notify_linear(tmp_path: Path) -> None:
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 500, ("TypeError",), "Error", None))
    completion = '[{"severity":"high","title":"500","detail":"server error"}]'
    linear_calls: list[tuple[str, str, str]] = []
    deps = _deps(
        tmp_path,
        qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=browse,
        completion=completion,
        linear_calls=linear_calls,
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert "500" in gh.comments[0]
    assert "/10" in gh.comments[0]
    assert len(linear_calls) == 1
    owner, pr_url, body = linear_calls[0]
    assert owner == "o"
    assert pr_url == "https://github.com/o/r/pull/7"
    assert "500" in body


async def test_linear_failure_does_not_break_qa(tmp_path: Path) -> None:
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 500, ("TypeError",), "Error", None))

    @contextlib.asynccontextmanager
    async def fake_open_url(_repo: Path, _qa: QAConfig) -> AsyncIterator[str]:
        yield "http://app.test"

    async def fake_complete(prompt: str, cfg: LLMConfig) -> str:
        return '[{"severity":"high","title":"boom","detail":"d"}]'

    async def fake_clone(_job: PRJob, _cfg: Config, _gh: object) -> Path:
        return tmp_path

    async def fake_cleanup(_repo: Path) -> None:
        return None

    async def exploding_notify(owner: str, pr_url: str, body: str) -> bool:
        raise RuntimeError("linear down")

    deps = QADeps(
        load_qa=lambda _root: RepoOverride(qa=QAConfig(enabled=True, url="http://app.test", start="x"), qa_enabled_set=True),
        clone=fake_clone,
        open_url=fake_open_url,
        browse=browse,
        complete=fake_complete,
        cleanup=fake_cleanup,
        notify_linear=exploding_notify,
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1
    assert "boom" in gh.comments[0]


async def test_url_resolution_failure_on_explicit_posts_did_not_run(tmp_path: Path) -> None:
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 200, (), "ok", None))

    @contextlib.asynccontextmanager
    async def failing_open_url(_repo: Path, _qa: QAConfig) -> AsyncIterator[str]:
        raise URLResolutionError("no server")
        yield ""  # pragma: no cover

    deps = _deps(
        tmp_path,
        qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=browse,
        completion="[]",
    )
    deps = QADeps(**{**deps.__dict__, "open_url": failing_open_url})
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1
    assert "Did not run" in gh.comments[0]
    assert "no server" in gh.comments[0]


async def test_unexpected_error_on_explicit_posts_error_comment(tmp_path: Path) -> None:
    gh = FakeGH()

    async def boom_complete(prompt: str, cfg: LLMConfig) -> str:
        raise RuntimeError("kaboom")

    deps = _deps(
        tmp_path,
        qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=FakeBrowse(PageState("http://app.test", 200, (), "ok", None)),
        completion="[]",
    )
    deps = QADeps(**{**deps.__dict__, "complete": boom_complete})
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1
    assert "unexpected error" in gh.comments[0]
    assert "kaboom" in gh.comments[0]


# --- org-wide auto behavior (new) -------------------------------------------


async def test_org_default_enabled_autodetect_runs(tmp_path: Path) -> None:
    _next_pkg(tmp_path)  # detectable Next.js app
    gh = FakeGH()
    deps = _deps(
        tmp_path,
        qa=QAConfig(),  # nothing set; start empty -> auto-detect
        browse=FakeBrowse(PageState("http://app.test", 200, (), "Welcome", None)),
        completion="[]",
        enabled_set=False,  # org-default enabled, not repo-explicit
    )
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    assert len(gh.comments) == 1
    assert "No issues found" in gh.comments[0]


async def test_auto_undetectable_skips_silently(tmp_path: Path) -> None:
    # No package.json / index.html -> detect returns None -> silent skip.
    gh = FakeGH()
    deps = _deps(
        tmp_path,
        qa=QAConfig(),
        browse=FakeBrowse(PageState("http://app.test", 200, (), "x", None)),
        completion="[]",
        enabled_set=False,
    )
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    assert gh.comments == []


async def test_auto_serve_failure_skips_silently(tmp_path: Path) -> None:
    _next_pkg(tmp_path)  # detectable, but the server won't come up
    gh = FakeGH()

    @contextlib.asynccontextmanager
    async def failing_open_url(_repo: Path, _qa: QAConfig) -> AsyncIterator[str]:
        raise URLResolutionError("port never opened")
        yield ""  # pragma: no cover

    deps = _deps(
        tmp_path, qa=QAConfig(), browse=FakeBrowse(PageState("http://app.test", 200, (), "x", None)),
        completion="[]", enabled_set=False,
    )
    deps = QADeps(**{**deps.__dict__, "open_url": failing_open_url})
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    assert gh.comments == []  # auto repo: no noise on serve failure


async def test_rs21_repo_never_runs(tmp_path: Path) -> None:
    _next_pkg(tmp_path)
    gh = FakeGH()
    # Even explicitly enabled, RS21 is hard-blocked.
    deps = _deps(
        tmp_path, qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=FakeBrowse(PageState("http://app.test", 200, (), "x", None)),
        completion="[]", enabled_set=True,
    )
    await process_qa_job(_job(owner="teamnebula-ai", repo="rs21-jira-mirror"),
                         _cfg(default_orgs=frozenset({"teamnebula-ai"})), gh, deps)
    assert gh.comments == []


async def test_owner_not_in_default_orgs_skips(tmp_path: Path) -> None:
    _next_pkg(tmp_path)
    gh = FakeGH()
    deps = _deps(
        tmp_path, qa=QAConfig(), browse=FakeBrowse(PageState("http://app.test", 200, (), "x", None)),
        completion="[]", enabled_set=False,
    )
    await process_qa_job(_job(owner="screddyice"), _cfg(default_orgs=frozenset({"teamnebula-ai"})), gh, deps)
    assert gh.comments == []
