# Design: Org-wide auto-QA + Fix mode

Status: DRAFT for review (design-first; no code yet)
Date: 2026-05-26
Supersedes scope deferred from `2026-05-25-pr-qa-mode-design.md` (M2 org defaults + M4 fix)

## Goal

Two capabilities on top of the shipped report-only QA (M1):

1. **Org-wide auto-QA** — QA runs on NEB + TRC repos without per-repo opt-in,
   figuring out how to build/serve each app itself, skipping silently where it
   can't, and never touching RS21.
2. **Fix mode (M4)** — when QA finds an issue, the bot applies the recommended
   fix, runs the verify gate, and opens a *new* PR with the fix plus a comment
   explaining each change. Report-only stays the default.

Decided with the user: rollout = org-wide (NEB + TRC); start command =
**auto-detect, skip silently when undetectable**; fix mode = open a **new PR**
(not push to the PR branch).

---

## Part A — Org-wide auto-QA

### A1. Enablement precedence (tri-state)

Today `[qa] enabled` is a bool defaulting false. To support an org default that
a repo can still override, `enabled` becomes tri-state at parse time:

- repo `.pr-conflict-bot.toml` sets `[qa] enabled` → that wins (true or false)
- not set in TOML → fall back to the **org default**
- org default = owner ∈ `QA_DEFAULT_ENABLED_ORGS` (new env, e.g.
  `teamnebula-ai,the-ready-consult`), lower-cased set, same shape as `ALLOW_ORGS`

Implementation: `QAConfig.enabled` stays bool for callers, but
`load_repo_override` records whether the key was present (e.g. an internal
`enabled_explicit: bool`), and the orchestrator resolves the effective value:
`effective = toml.enabled if explicit else (owner in default_orgs)`.

### A2. RS21 hard-exclusion

Repos whose **name** contains `rs21` (case-insensitive) are never auto-enabled
and never QA'd, even if a TOML says `enabled = true`. Mirrors the existing
hardcoded `DENY_ORGS` (cliqk) pattern — a force-off check in the orchestrator
before anything runs. Honors the workspace RS21 lockdown.

### A3. Auto-detect build/serve

When QA is enabled but `[qa] start`/`url` are empty, infer them from the clone:

| Signal | start | url (default) |
|---|---|---|
| dep `next` | `<pm> run build` then `<pm> run start` | `http://localhost:3000` |
| dep `vite` | `<pm> run build` then `<pm> run preview` | `http://localhost:4173` |
| dep `react-scripts` | `<pm> start` | `http://localhost:3000` |
| a `dev` script, else | `<pm> run dev` | `http://localhost:3000` |
| root `index.html`, no package.json | `python3 -m http.server <port>` | that port |
| nothing detectable | **skip silently (no comment)** | — |

`<pm>` from lockfile: `bun.lockb`→bun, `pnpm-lock.yaml`→pnpm, `yarn.lock`→yarn,
else npm. An explicit `[qa] start` in the repo always overrides detection.

### A4. Silent-skip vs. report-failure (noise control)

- **Auto-enabled** repo (org default) that can't be served (no detectable
  command, build fails, port never comes up) → **skip silently**, no PR comment.
  The owner didn't ask for QA; don't spam them.
- **Explicitly-configured** repo (`[qa] start` present) that fails to serve →
  post the existing "Did not run" comment. The owner opted in; tell them.

### A5. Known limitations (accepted)

- Apps needing secrets/env to boot won't come up → silent skip. Coverage is
  partial by design.
- Port is guessed per framework; if the app binds elsewhere, readiness times out
  → silent skip. (Future: parse the dev server's printed URL from stdout.)
- Build cost: `npm ci` + build + serve per PR is heavy. Needs per-job timeouts
  and a concurrency cap (see Risks).

---

## Part B — Fix mode (M4)

Gated on `[qa] mode = "fix"` (the slot already exists; default stays `report`).

### B1. Flow (only when findings exist, mode=fix, not RS21)

1. Findings already computed (report path runs first; the PR comment still posts).
2. **Apply fixes**: an editing LLM call (reuse `llm`'s edit path, not the
   read-only `complete`) takes the findings + relevant file contents and edits
   the clone. New prompt in `methodology` (`build_fix_prompt`).
3. **Verify gate**: run `verify.py` (lint/typecheck/test from `[verify]`/env).
   Fail → no PR; comment: "QA found N issues and attempted a fix, but the verify
   gate failed — not opening a PR. Details: …". The gate is the safety net.
4. **Open a new PR**: branch `qa-fix/<pr-branch>-<shortsha>` off the PR head,
   commit, push, open a PR with **base = the original PR's head branch** (so
   merging the fix PR updates the feature PR). Requires a new
   `github_api.create_pull_request`.
5. **Explain**: comment on the *original* PR linking the fix PR and listing each
   change mapped to the finding it addresses ("so we know why it did what it did").

### B2. RS21 hard-block

`mode="fix"` on an RS21 repo → never edit/push. Fall back to report-only and note
fix mode is disabled there.

### B3. Reuse vs. new

- Reuse: `git_ops` (clone, branch, push-with-lease), `verify.py` (gate),
  `llm` (editing).
- New: `github_api.create_pull_request`; `methodology.build_fix_prompt`;
  fix branch/PR orchestration in `qa/orchestrator.py`.

### B4. Blast-radius decision — DECIDED: org-wide fix from day one

User decision (2026-05-26): **org-wide fix from day one.** The org default for
NEB + TRC is `enabled = true` **and** `mode = "fix"`. On every PR in every
servable NEB/TRC repo (RS21 excluded), QA runs and, on findings, auto-opens a
fix PR. A repo can still downgrade itself with `[qa] mode = "report"` or opt out
with `[qa] enabled = false`.

Guards (the only things between this and chaos — they are load-bearing):
- **Verify gate must pass** before any fix PR is opened. No gate pass → no PR.
- **Never auto-merge.** Every fix lands as a PR a human reviews.
- **RS21 hard-blocked** regardless of config.
- New `QA_DEFAULT_MODE` env (default `fix` for the auto-enabled orgs) so the
  blast radius is reversible from config without a redeploy.
- Per-repo cooldown + global concurrency cap (see Risks) to bound cost/load.

---

## Risks

- **Resource/cost**: build+serve+judge (+fix) per PR across all repos. Needs
  per-job timeout, a global QA concurrency cap, and likely a per-repo cooldown.
- **Auto-detect false positives**: wrong start command → wasted build, silent
  skip on timeout. Acceptable, but watch the logs early.
- **Fix quality**: verify gate + human review of the fix PR are the guards. Never
  auto-merge.
- **Secrets**: the bot won't have app secrets; secret-needing apps silently skip.
- **`next start` needs a build**; detection must chain build→start or the server
  won't boot.

## Out of scope

- Auto-merging fix PRs (never).
- Cliqk (hardcoded `DENY_ORGS`).
- Diff-aware multi-page QA / tiers / lenses (still M3).
