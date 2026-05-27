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
from pr_conflict_bot.qa.orchestrator import FixOutcome, QADeps, RunFixFn, process_qa_job
from pr_conflict_bot.qa.url_resolver import URLResolutionError
from pr_conflict_bot.server import PRJob
from pr_conflict_bot.verify import VerifyResult


class FakeGH:
    def __init__(self) -> None:
        self.comments: list[str] = []

    async def clone_url(self, installation_id: int, owner: str, repo: str) -> str:
        return "file:///unused"

    async def post_issue_comment(
        self, installation_id: int, owner: str, repo: str, pr_number: int, body: str
    ) -> None:
        self.comments.append(body)

    async def list_self_comment_bodies(
        self, installation_id: int, owner: str, repo: str, pr_number: int
    ) -> list[str]:
        return list(self.comments)


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
    run_fix: RunFixFn | None = None,
    diff_text: str = "+ added a line\n",
    verify_passed: bool = True,
) -> QADeps:
    @contextlib.asynccontextmanager
    async def fake_open_url(_repo: Path, _qa: QAConfig) -> AsyncIterator[str]:
        yield "http://app.test"

    async def fake_complete(prompt: str, cfg: LLMConfig) -> str:
        return completion

    async def fake_pr_diff(_repo: Path, _base: str) -> str:
        return diff_text

    async def fake_verify(_cfg: object, _repo: Path) -> VerifyResult:
        return VerifyResult(passed=verify_passed, steps=())

    async def fake_clone(_job: PRJob, _cfg: Config, _gh: object) -> Path:
        return repo_dir

    async def fake_cleanup(_repo: Path) -> None:
        return None

    async def fake_notify_linear(owner: str, pr_url: str, body: str) -> bool:
        if linear_calls is not None:
            linear_calls.append((owner, pr_url, body))
        return True

    async def _default_run_fix(
        job: PRJob, cfg: object, gh: object, repo_dir: Path, state: object, findings: list[object]
    ) -> FixOutcome:
        return FixOutcome(changed=False, verified=False, pushed=False, detail="")

    return QADeps(
        load_qa=lambda _root: RepoOverride(qa=qa, qa_enabled_set=enabled_set, qa_mode_set=enabled_set),
        clone=fake_clone,
        open_url=fake_open_url,
        browse=browse,
        complete=fake_complete,
        cleanup=fake_cleanup,
        notify_linear=fake_notify_linear,
        run_fix=run_fix or _default_run_fix,  # type: ignore[arg-type]
        pr_diff=fake_pr_diff,
        run_verify=fake_verify,
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


# --- fix safety: bot artifacts must not be committed onto the PR ------------


async def test_screenshot_written_outside_the_clone(tmp_path: Path) -> None:
    # In fix mode the LLM's edits are committed with `git add -A` and pushed to
    # the PR branch. A screenshot written inside the clone would be committed
    # onto the PR — so it must live outside repo_dir.
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 200, (), "Welcome", None))
    deps = _deps(
        tmp_path,  # this is repo_dir (fake_clone returns it)
        qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=browse,
        completion="[]",
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert browse.screenshot_targets, "screenshot target was never set"
    shot = browse.screenshot_targets[0]
    assert shot is not None
    assert tmp_path not in shot.parents  # not inside the clone


# --- idempotency: don't re-review the same head sha -------------------------


async def test_skips_when_head_sha_already_reviewed(tmp_path: Path) -> None:
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 200, (), "Welcome", None))
    deps = _deps(
        tmp_path,
        qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=browse,
        completion="[]",
    )
    # First run reviews the page and posts one comment (with the reviewed-sha marker).
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1
    assert len(browse.captured_urls) == 1

    # Second delivery for the SAME head sha must be skipped: no new comment, and
    # the page is not captured/judged again.
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1
    assert len(browse.captured_urls) == 1


async def test_new_head_sha_is_reviewed_again(tmp_path: Path) -> None:
    gh = FakeGH()
    browse = FakeBrowse(PageState("http://app.test", 200, (), "Welcome", None))
    deps = _deps(
        tmp_path,
        qa=QAConfig(enabled=True, url="http://app.test", start="x"),
        browse=browse,
        completion="[]",
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert len(gh.comments) == 1

    # A new commit (different head sha) is a different review — not deduped.
    from dataclasses import replace

    await process_qa_job(replace(_job(), pr_head_sha="def456"), _cfg(), gh, deps)
    assert len(gh.comments) == 2


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


# --- fix mode ---------------------------------------------------------------

_FINDING = '[{"severity":"high","title":"bug","detail":"d"}]'


def _findings_browse() -> FakeBrowse:
    return FakeBrowse(PageState("http://app.test", 500, ("E",), "err", None))


async def test_fix_mode_pushes_to_pr_and_comments(tmp_path: Path) -> None:
    gh = FakeGH()
    calls: list[int] = []

    async def fake_run_fix(job, cfg, gh_, repo_dir, state, findings):  # type: ignore[no-untyped-def]
        calls.append(len(findings))
        return FixOutcome(changed=True, verified=True, pushed=True, detail="")

    deps = _deps(
        tmp_path, qa=QAConfig(enabled=True, mode="fix", url="http://app.test", start="x"),
        browse=_findings_browse(), completion=_FINDING, run_fix=fake_run_fix,
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert calls == [1]
    assert any("pushed them to this PR" in c for c in gh.comments)


async def test_fix_mode_verify_fail_no_push(tmp_path: Path) -> None:
    gh = FakeGH()

    async def fake_run_fix(job, cfg, gh_, repo_dir, state, findings):  # type: ignore[no-untyped-def]
        return FixOutcome(changed=True, verified=False, pushed=False, detail="[FAIL] test")

    deps = _deps(
        tmp_path, qa=QAConfig(enabled=True, mode="fix", url="http://app.test", start="x"),
        browse=_findings_browse(), completion=_FINDING, run_fix=fake_run_fix,
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert any("verify gate failed" in c and "nothing pushed" in c for c in gh.comments)
    assert not any("pushed them to this PR" in c for c in gh.comments)


async def test_fix_mode_no_edits_only_report(tmp_path: Path) -> None:
    gh = FakeGH()

    async def fake_run_fix(job, cfg, gh_, repo_dir, state, findings):  # type: ignore[no-untyped-def]
        return FixOutcome(changed=False, verified=False, pushed=False, detail="")

    deps = _deps(
        tmp_path, qa=QAConfig(enabled=True, mode="fix", url="http://app.test", start="x"),
        browse=_findings_browse(), completion=_FINDING, run_fix=fake_run_fix,
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    # Only the report comment — no fix-PR / verify-fail comment.
    assert len(gh.comments) == 1
    assert "bug" in gh.comments[0]


# --- code-level QA (non-web repos) ------------------------------------------


async def test_code_qa_findings_post_comment(tmp_path: Path) -> None:
    # No package.json / index.html -> not a web app -> code QA reviews the diff.
    gh = FakeGH()
    deps = _deps(
        tmp_path, qa=QAConfig(), browse=FakeBrowse(PageState("x", 200, (), "x", None)),
        completion='[{"severity":"high","title":"null deref","detail":"foo.py"}]',
        enabled_set=False,
    )
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    assert len(gh.comments) == 1
    assert "code review" in gh.comments[0]
    assert "null deref" in gh.comments[0]


async def test_code_qa_clean_diff_is_silent(tmp_path: Path) -> None:
    gh = FakeGH()
    deps = _deps(
        tmp_path, qa=QAConfig(), browse=FakeBrowse(PageState("x", 200, (), "x", None)),
        completion="[]", enabled_set=False,
    )
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    # Clean code review must NOT comment (no org-wide per-PR noise).
    assert gh.comments == []


async def test_code_qa_empty_diff_is_silent(tmp_path: Path) -> None:
    gh = FakeGH()
    deps = _deps(
        tmp_path, qa=QAConfig(), browse=FakeBrowse(PageState("x", 200, (), "x", None)),
        completion='[{"severity":"high","title":"x","detail":"y"}]', enabled_set=False,
        diff_text="   \n",  # nothing changed
    )
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    assert gh.comments == []


async def test_code_qa_fix_mode_runs_fix(tmp_path: Path) -> None:
    gh = FakeGH()
    calls: list[str] = []

    async def fake_run_fix(job, cfg, gh_, repo_dir, fix_prompt, findings):  # type: ignore[no-untyped-def]
        calls.append(fix_prompt)
        return FixOutcome(changed=True, verified=True, pushed=True, detail="")

    deps = _deps(
        tmp_path, qa=QAConfig(enabled=True, mode="fix"), browse=FakeBrowse(PageState("x", 200, (), "x", None)),
        completion='[{"severity":"high","title":"bug","detail":"d"}]',
        enabled_set=True, run_fix=fake_run_fix,
    )
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    assert len(calls) == 1  # fix ran with a (diff-based) prompt
    assert any("pushed them to this PR" in c for c in gh.comments)


async def test_code_qa_verify_failure_becomes_finding(tmp_path: Path) -> None:
    # LLM finds nothing, but the repo's verify gate fails -> that's a finding.
    gh = FakeGH()
    deps = _deps(
        tmp_path, qa=QAConfig(), browse=FakeBrowse(PageState("x", 200, (), "x", None)),
        completion="[]", enabled_set=True, verify_passed=False,
    )
    # Give the repo a real verify gate via the override so the gate actually runs.
    base = deps
    deps = QADeps(**{**base.__dict__,
                    "load_qa": lambda _r: RepoOverride(qa=QAConfig(enabled=True), qa_enabled_set=True,
                                                       qa_mode_set=True, verify=VerifyConfig(test="pytest"))})
    await process_qa_job(_job(owner="o"), _cfg(default_orgs=frozenset({"o"})), gh, deps)
    assert len(gh.comments) == 1
    assert "verify gate failing" in gh.comments[0]


async def test_report_mode_does_not_run_fix(tmp_path: Path) -> None:
    gh = FakeGH()
    called: list[int] = []

    async def fake_run_fix(job, cfg, gh_, repo_dir, state, findings):  # type: ignore[no-untyped-def]
        called.append(1)
        return FixOutcome(changed=True, verified=True, pushed=True, detail="")

    deps = _deps(
        tmp_path, qa=QAConfig(enabled=True, mode="report", url="http://app.test", start="x"),
        browse=_findings_browse(), completion=_FINDING, run_fix=fake_run_fix,
    )
    await process_qa_job(_job(), _cfg(), gh, deps)
    assert called == []  # report mode never invokes fix
