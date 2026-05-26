# Design: QA Mode for pr-conflict-bot

- **Date:** 2026-05-25
- **Status:** Approved (design); implementation plan pending
- **Author:** Shawn Reddy (with Claude Code)
- **Repo:** `Screddyice/pr-conflict-bot` (MIT, public)

## Summary

Add a **QA mode** to the existing `pr-conflict-bot` GitHub App. Today the bot
fires on `pull_request` events and resolves merge conflicts using a pluggable
LLM backend (`claude` | `codex`), gated by a verify step, pushing the resolution
back for human review. QA mode adds a **second, independent flow**: on a pull
request, run an automated QA pass against the PR's running app — plan, drive a
real headless browser, triage findings — then post a findings comment and a
pass/fail GitHub Check. Code fixes are **opt-in per repo** and reuse the bot's
existing verify + force-with-lease push path.

This is delivered as new code inside the existing repo, not a new project. The
"QA harness for any model" requirement is satisfied by reusing the bot's
existing `llm.py` backend abstraction — the model is already pluggable.

### Why pr-conflict-bot is the vehicle

- It is already a GitHub App that fires on `pull_request` events, deployed on
  neb-server, installed on exactly the orgs in scope: `teamnebula-ai`,
  `The-Ready-Consult`, and `Screddyice` (the operator's own repos).
- Its `llm.py` already dispatches agentic work to `claude -p` / `codex exec`
  with an identical contract — this *is* the "any model" backend. No new
  backend code is required.
- Its scoping model already expresses the desired policy: `DENY_ORGS` hardcodes
  `mycliqk`/`cliqk` (Cliqk excluded), `ALLOW_ORGS`/`ALLOW_USERS` narrow firing,
  and `.pr-conflict-bot.toml` `[behavior] enabled = false` is how RS21 repos
  opt out of all automation.

## Goals

- Run an automated QA pass on PRs to NEB, TRC, and the operator's own repos.
- Be **model-agnostic**: reasoning runs through the existing `claude` | `codex`
  backends with no per-token API cost.
- Support **all users** of the OSS bot, not just the operator's setup — in
  particular, get a testable URL across **Vercel, Netlify, and Firebase
  Hosting** preview deploys via one provider-agnostic mechanism.
- Default to **safe, report-only** behavior on shared org PRs.
- Leave the existing conflict-resolution flow completely unchanged.

## Non-Goals

- No standalone CLI / separate repo (an earlier idea, dropped).
- No local-machine / pre-push runner — server-side GitHub App only.
- No auto-merge. The bot remains a janitor, never an approver. Human review via
  branch protection is still required.
- No support for Cliqk (`mycliqk`/`cliqk`) — excluded by `DENY_ORGS`.
- QA does **not** run on RS21 repos in fix mode under any configuration.

## Reuse vs. New

| Reused as-is | New for QA |
|---|---|
| `server.py` webhook receiver + HMAC verify + in-mem queue | a QA job variant + `process_qa_job` orchestrator |
| `git_ops` PR-head clone | live-URL resolver (preview-detect + start-command) |
| `llm.py` (`claude` \| `codex`) agentic backend | bundled `browse` engine (forked gstack, MIT) |
| `config.py` scoping (`DENY_ORGS`, `ALLOW_ORGS/USERS`) + `.pr-conflict-bot.toml` | `[qa]` config block |
| `verify.py` lint/typecheck/test gate | QA report + GitHub Check formatting |
| `github_api` install-token auth, PR comments | GitHub **Check run** create/update |

## Architecture

```
GitHub App (pull_request opened/synchronize/reopened, deployment_status)
   │  HMAC verify, classify event
   ▼
server.py ── enqueue ──► in-mem queue ──► worker
                                            │
              ┌─────────────────────────────┴─────────────────────────┐
              ▼                                                         ▼
      process_job (existing)                                  process_qa_job (new)
      conflict resolution flow                                QA flow (below)
      — UNCHANGED —
```

QA flow (`process_qa_job`):

```
1. resolve live URL
     deployment_status.target_url  →  deploy-bot comment parse  →  [qa] start command
     (none) → skip with explanatory comment (not a failure)
2. clone PR head (git_ops)           # for diff-aware scope + fix mode
3. launch browse engine              # Bun subprocess, headless Chromium
4. PLAN    → diff-aware test plan          (llm.py judgment call)
5. EXECUTE → drive browser; screenshots, console, network, perf samples
6. TRIAGE  → severity-classified findings  (llm.py judgment call)
7. [opt-in, mode = "fix"] FIX → llm.py agentic edit → verify gate → push-with-lease
8. REPORT  → PR comment + pass/fail GitHub Check (+ .qa artifacts in workdir)
```

The conflict flow and QA flow are independent jobs derived from the same
webhook delivery; either may run, neither blocks the other.

### Components

- **`server.py` (edit):** classify incoming events. `pull_request`
  opened/synchronize/reopened enqueues both a conflict job (existing) and, if QA
  is enabled for the repo, a QA job. `deployment_status` events are cached
  (keyed by PR head SHA / branch) so an in-flight QA job can resolve its URL.
- **`qa/` package (new):**
  - `orchestrator.py` — `process_qa_job`, the phase state machine above.
  - `url_resolver.py` — provider-agnostic live-URL resolution (see below).
  - `browse.py` — thin Python wrapper that launches and talks to the vendored
    `browse` engine as a subprocess; normalizes navigate/click/screenshot/
    console/network/perf into Python calls.
  - `methodology.py` — phase prompt templates extracted from gstack's
    `qa`/`qa-only`/`design-review` SKILL.md, stripped of Claude-Code-only
    constructs (`{{PREAMBLE}}`, `allowed-tools`, the `Skill` tool). Engine-neutral.
  - `report.py` — health score, findings → Markdown comment + Check run summary,
    `.qa/` artifact layout, regression/canary baselines.
- **`vendor/browse/` (new):** gstack's `browse` engine forked in (MIT). `NOTICE`
  file credits Garry Tan. Pinned; not a live dependency on a gstack install.
- **`llm.py` (reuse, minor extension):** the QA flow calls the same
  `claude -p` / `codex exec` wrappers. PLAN/TRIAGE use one-shot prompts;
  EXECUTE-exploration and FIX use agentic sessions with the repo as cwd.
- **`config.py` (extend):** parse a new `[qa]` block from
  `.pr-conflict-bot.toml`; enforce the RS21 `mode = "fix"` hard-block.
- **`github_api.py` (extend):** create/update a GitHub **Check run** for the QA
  result, in addition to the existing PR comment.

## Live-URL resolution (provider-agnostic)

QA needs a running app. Resolution order:

1. **`deployment_status` event** — read `target_url` where the deployment
   `environment` indicates a preview/PR environment. Vercel, Netlify, and the
   Firebase Hosting GitHub Action all emit a GitHub `deployment_status` with a
   `target_url`, so this single mechanism covers all three providers for all
   users. The bot caches recent `deployment_status` payloads and correlates by
   PR head SHA (preferred) or branch.
2. **Deploy-bot comment parse** — fallback when no usable `deployment_status`:
   extract the preview URL from `vercel[bot]` / `netlify[bot]` / Firebase action
   PR comments via known patterns.
3. **Per-repo start command** — final fallback: `[qa] build/start/url` from
   `.pr-conflict-bot.toml`; build and serve the PR checkout in the work dir,
   run QA against `url`, tear the server down afterward.
4. **None available** — skip the QA job and post a short explanatory comment.
   This is a skip, not a failure (no failing Check).

Preview deploys are typically asynchronous. A QA job that finds no URL yet waits
for a correlating `deployment_status` up to a bounded timeout before falling
through to comment-parse / start-command / skip.

## Configuration (`.pr-conflict-bot.toml`)

```toml
[qa]
enabled = true            # default OFF; QA is strictly opt-in per repo
mode = "report"           # "report" | "fix"  (fix is hard-blocked on rs21 repos)
tier = "standard"         # "quick" | "standard" | "exhaustive"
lens = ["functional"]     # any of "functional" | "design" | "perf"
# start-command fallback (only used when no preview deploy is detected):
url = "http://localhost:3000"
start = "npm run dev"
build = "npm run build"
```

`lens` selects which gstack methodologies apply: `functional` = `/qa`-style
test→triage(→fix); `design` = `/design-review` visual pass; `perf` =
`/benchmark` Core Web Vitals / load-time sampling with a stored baseline.
(`/canary`-style post-deploy monitoring is a future lens, out of scope for this
spec's milestones.)

Server-level env (existing): `LLM_BACKEND`, `ALLOW_ORGS`, `ALLOW_USERS`,
`DENY_ORGS`, `REQUIRE_REPO_CONFIG`. No new required env; QA reuses the selected
backend's auth.

## Safety & scope

- **Cliqk excluded** — `DENY_ORGS` (`mycliqk`/`cliqk`) already overrides
  everything; QA inherits this.
- **RS21 excluded** — repos opt out with `[behavior] enabled = false`.
  Additionally, `[qa] mode = "fix"` is hard-blocked for any repo whose
  org/name matches the RS21 rule, regardless of config.
- **Opt-in** — `[qa] enabled` defaults to off. No repo runs QA until it opts in,
  matching today's per-repo override model.
- **Report-only default** — `mode = "report"` posts a comment + Check and never
  touches code. `mode = "fix"` reuses the existing verify gate and
  `push-with-lease`; it never auto-merges and never counts as a reviewer.
- **No unverified pushes** — fix mode obeys the existing mandatory verify gate;
  on verify failure it posts findings and pushes nothing.

## GitHub App migration

Permission/event changes required (manifest update + re-approval on the three
orgs):

- Repository → **Checks: Read & write** (currently Read).
- Subscribe to **Deployment statuses** (and **Deployments**) events.

`scripts/setup-github-app.py` is updated to request these in the manifest. A
short migration note in the README covers re-approving the existing install.

## Testing strategy

- **`FakeBrowse` + `FakeLLM`** test doubles so the QA orchestrator is fully
  testable without a real browser or model. Assert phase transitions, report
  shape, Check status, and skip/failure paths.
- **Fixture app:** a tiny deliberately-buggy web app under `tests/fixtures/`
  served locally; `process_qa_job` runs end-to-end in report mode via the
  start-command path and asserts findings + comment + Check.
- **URL resolver unit tests:** synthetic `deployment_status` payloads for
  Vercel/Netlify/Firebase shapes; comment-parse patterns; start-command
  fallback; correlation by SHA vs branch; timeout → skip.
- **Config tests:** `[qa]` parsing, defaults, RS21 `mode=fix` hard-block.
- Port relevant gstack `browse` tests with the vendored engine.
- Existing gates unchanged: `ruff check .`, `mypy src`, `pytest`.

## Error handling

- doctor/setup checks for **Bun + headless Chromium + browse engine** on the
  host; hard-fail early with actionable messages if missing.
- Per-phase timeouts; bounded retries on the fix loop (mirror
  `max_review_redispatches`).
- A failed QA phase posts a findings comment with what completed and sets a
  **failing Check** — never silent. URL-not-found is a *skip* (neutral Check or
  no Check + explanatory comment), not a failure.
- Browser/engine crash is caught, reported, and cleaned up; the work dir is
  always torn down in `finally` (same pattern as `process_job`).

## Deployment dependencies (neb-server)

- Install **Bun** and a **headless Chromium** the vendored `browse` engine
  drives.
- The systemd unit gains those on PATH; `pr-conflict-bot` setup verifies them.

## Milestones

- **M1 — QA plumbing + report-only via start command.** `qa/` package,
  `process_qa_job`, `[qa]` config, vendored `browse` + Bun/Chromium on
  neb-server, report mode against the start-command URL. Testable on the fixture
  app with `FakeLLM`.
- **M2 — Provider-agnostic preview detect.** `deployment_status.target_url`
  (Vercel/Netlify/Firebase) + comment-parse fallback + SHA/branch correlation +
  wait-with-timeout.
- **M3 — Check run + richer methodology.** GitHub Check create/update,
  diff-aware test plan, tiers, and `design`/`perf` lenses.
- **M4 — Opt-in fix mode.** `mode = "fix"` reusing verify + push-with-lease,
  RS21-blocked.

## Open questions (resolve at plan time)

- Exact health-score formula carried over from gstack vs. simplified for CI.
- Where QA artifacts (screenshots) are hosted for the PR comment — inline Check
  summary, committed to a branch, or uploaded as run artifacts.
- Whether `synchronize` re-runs full QA or a diff-scoped subset to bound cost.
