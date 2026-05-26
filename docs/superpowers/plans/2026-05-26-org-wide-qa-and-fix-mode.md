# Implementation Plan: Org-wide auto-QA + Fix mode

> Spec: `docs/superpowers/specs/2026-05-26-org-wide-qa-and-fix-mode.md`
> TDD: write the failing test first for each task, then implement, then lint+type+commit.
> Branch: `feat/qa-org-wide-fix`

## Task 1 — Config: tri-state enable, org defaults, RS21, resolver
- `config.py`: `RepoOverride` records `qa_enabled_set` / `qa_mode_set` (was the key present in TOML).
- `Config`: add `qa_default_enabled_orgs: frozenset[str]` (env `QA_DEFAULT_ENABLED_ORGS`) and `qa_default_mode: str` (env `QA_DEFAULT_MODE`, code-default `"report"` — server sets `fix`).
- New `qa_policy.py`: `is_rs21(owner, repo) -> bool` (name contains "rs21", case-insensitive); `resolve_qa(override, cfg, owner, repo) -> QAConfig` applying precedence: RS21 → force `enabled=False`; else `enabled = override.qa.enabled if qa_enabled_set else owner in qa_default_enabled_orgs`; `mode = override.qa.mode if qa_mode_set else qa_default_mode`. mode forced to `"report"` on RS21 belt-and-suspenders.
- Tests: `test_qa_policy.py` — rs21 off even when enabled; org-default on; repo opt-out wins; mode default vs explicit.

## Task 2 — Auto-detect build/serve
- New `qa/detect.py`: `detect_serve(repo_dir) -> QAConfig-ish (start,url,build) | None`. Reads `package.json` deps + lockfile (pm), root `index.html`. Returns None when undetectable.
- Tests: `test_qa_detect.py` — next/vite/react-scripts/dev/static/none; pm from lockfile.

## Task 3 — Orchestrator wiring (report path)
- `process_qa_job`: use `resolve_qa(...)` for enabled/mode. When enabled and `[qa] start` empty → `detect_serve`; None → **silent skip** (no comment, no error). Auto-enabled + serve-failure → silent skip; explicitly-configured + failure → existing "Did not run".
- Tests: extend `test_qa_orchestrator.py` — org-default-enabled clone with detectable app runs; undetectable → no comment; rs21 → no comment.

## Task 4 — Fix mode: apply + verify + new PR
- `methodology.build_fix_prompt(state, findings)`.
- `llm`: an editing call (reuse conflict edit path; not read-only `complete`).
- `verify.py`: reuse the gate. Fix must pass or no PR.
- `github_api.create_pull_request(installation_id, owner, repo, head, base, title, body) -> url`.
- `git_ops`: branch `qa-fix/<pr-branch>-<sha>`, commit, push.
- `orchestrator`: when `mode=="fix"` and findings and not RS21 → apply → verify → push branch → open PR (base = PR head branch) → comment on original PR linking the fix PR + per-finding explanation. Injected via QADeps (`apply_fix`, `open_fix_pr`) so it's testable with fakes.
- Tests: `test_qa_fix.py` + orchestrator — verify-fail → no PR + comment; success → fix PR opened + explanatory comment; rs21 → never edits.

## Task 5 — Safety rails
- Per-job timeout on build/serve already via url_resolver `ready_timeout`; add global QA concurrency cap (semaphore in qa_worker pool) + per-repo cooldown (in-memory last-run map) to bound cost.
- Tests for cooldown/concurrency where practical.

## Task 6 — Docs + env + deploy
- README: org-wide section, `QA_DEFAULT_ENABLED_ORGS`, `QA_DEFAULT_MODE`, auto-detect, RS21 note, fix-PR flow.
- `.env.example`: new vars.
- Deploy: merge, pull on neb-server, set `QA_DEFAULT_ENABLED_ORGS=teamnebula-ai,the-ready-consult`, `QA_DEFAULT_MODE=fix`, `LINEAR_TOKENS`, restart. Watch first real NEB PR.
