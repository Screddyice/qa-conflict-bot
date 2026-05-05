# pr-conflict-bot — v0.1 spec

**Status:** Spec locked, ready for legion dispatch.
**Mode:** SCOPE EXPANSION (pivoted from HOLD SCOPE after outside-voice review on 2026-05-05).
**Owner:** Screddyice OSS · github.com/Screddyice/pr-conflict-bot
**Deploy target:** NEB EC2 (initial); NEB org (rollout target); TRC org (later — Cliqk excluded per memory)

## Goal

GitHub App that **proactively writes fix branches** for PR merge conflicts before any human merge attempt. Routes between OpenAI API (codex models) and Claude Code SDK based on conflict classification. Verifies every proposed resolution actually merges cleanly **before** publishing — never posts an unverified suggestion.

Explicitly: **this is a fixer, not a suggester.** Bot pushes a `bot/resolve-<sha>` branch with the resolution and posts a PR comment with a "merge this fix" link. Humans approve with one click.

## Architecture

```
GITHUB ORG (NEB / TRC / Screddyice)
  │
  │  webhook events: push (default branch), pull_request.opened/synchronize/reopened
  │
  ▼
[1] WEBHOOK RECEIVER  (separate aiohttp process from ~/webhook-receiver/receiver.py)
      verify GitHub App signature → enqueue → 200 immediately
  │
  ▼
[2] JOB QUEUE  (sqlite-backed, persistent dedupe)
      key = (installation_id, repo, pr_number, head_sha, base_sha)
      TTL: 7 days
  │
  ▼
[3] WORKER POOL  (concurrency-capped at MAX_PARALLEL=4)
      legion-decomposable: each worker handles 1 PR end-to-end
  │
  │  per worker:
  │
  ├─ [4] PR CHECKOUT    cached bare-repo at ~/.cache/pr-conflict-bot/<repo>.git
  │                     worktree-add for the PR head_sha (no full clone)
  ├─ [5] LOCAL MERGE    git merge --no-ff base into PR worktree
  │                     if clean: bot exits (no conflict to fix)
  ├─ [6] EXTRACT        parse conflict markers from `git diff --check` + file scan
  ├─ [7] CLASSIFY       heuristic router (file count, hunk size, semantic hints)
  ├─ [8] LLM CALL       OpenAI API (gpt-codex models) OR Claude Code SDK
  │                     authenticated via dedicated keys (NOT codex CLI session)
  ├─ [9] VERIFY         apply patch to scratch worktree
  │                     git merge --no-commit
  │                     run repo's CI command if defined (gh repo view --json defaultBranchRef)
  │                     if verify fails: try other engine (1 fallback), then escalate
  ├─ [10] PUBLISH       on verify success:
  │                       push bot/resolve-<sha> branch via installation token
  │                       post PR comment with one-click merge link + diff summary
  │                     on verify fail (after fallback):
  │                       post "conflict needs human resolution" comment + classification reason
  └─ [11] LOG           structured journald entry (request_id, repo, PR, conflict_files,
                        engine, classification, verify_outcome, duration_ms, tokens, cost_usd)
```

## Decisions (final)

| # | Decision | Value |
|---|----------|-------|
| 1 | **Trigger** | Proactive: `push` to default + `pull_request.opened/synchronize/reopened` |
| 2 | **Bot type** | GitHub App (multi-org, auto-token-minted per installation) |
| 3 | **Bot identity** | App's `pr-conflict-bot[bot]@users.noreply.github.com` |
| 4 | **Action** | **Branch-commit + PR comment with merge link** (NOT comment-only) |
| 5 | **Verification** | Mandatory before publish: apply patch → `git merge --no-commit` → repo CI cmd if defined |
| 6 | **OpenAI auth** | OpenAI API key direct (NOT Codex CLI session) — `OPENAI_API_KEY` in `~/projects/.env` |
| 7 | **Anthropic auth** | Claude Code SDK with `ANTHROPIC_API_KEY` (already in `~/projects/.env`) |
| 8 | **Engine routing** | Heuristic classifier (see below); engine fallback on verify failure (codex → claude) |
| 9 | **Concurrency** | `MAX_PARALLEL=4` workers; sqlite-backed queue handles bursts |
| 10 | **Process model** | **Separate worker process** from `~/webhook-receiver/receiver.py` (don't block other handlers) |
| 11 | **Repo caching** | Bare-repo cache at `~/.cache/pr-conflict-bot/<repo>.git`; worktrees per invocation |
| 12 | **Idempotency** | Persistent sqlite key `(installation_id, repo, pr, head_sha, base_sha)`, 7-day TTL |
| 13 | **Escalation** | After 1 engine retry + 1 fallback engine: post "needs human" comment, halt |
| 14 | **Webhook auth** | `PR_CONFLICT_BOT_WEBHOOK_SECRET` in `~/projects/.env`, signature verified per request |
| 15 | **Deploy** | NEB EC2, systemd `--user` unit `pr-conflict-bot.service` |
| 16 | **Observability** | journald structured logs; queries via `journalctl --user -u pr-conflict-bot \| jq` |

## Classification heuristic (v0.1)

| Conflict shape | Engine | Why |
|---|---|---|
| Single file, single hunk, <50 lines, no signature changes | OpenAI codex | Fast, cheap, narrow context fits |
| Multi-file conflict | Claude Code | Cross-file reasoning |
| Single file >50 lines OR touches function signatures | Claude Code | Larger context, semantic |
| Pure import/dependency block conflicts | OpenAI codex | Mechanical, well-bounded |
| Default (anything else) | Claude Code | Bias to richer model when uncertain |

Classifier outputs route + reason. Reason logged. **No "low confidence" branch** — if engine fails verification, we fall back to the other engine (defined behavior, not derived from classifier).

## Out of scope (v0.1)

- ML-based classifier (heuristic only)
- Per-repo configuration files
- Slack/email notifications
- Web dashboard / metrics aggregation UI
- Auto-merge after fix lands (bot opens fix branch + comment; humans merge)
- Cliqk org installation (deliberate exclusion per memory)
- Multi-language AST-aware parsing (treat all conflicts as text + structural markers)
- Conflict resolution learning from past resolutions (no memory loop)

## Failure surface (closed in spec)

| Risk | Mitigation in v0.1 |
|---|---|
| Webhook spoofing | HMAC signature verification per request |
| API key leak | All keys in `~/projects/.env` (gitignored), App private key chmod 600 |
| LLM prompt injection via PR content | Resolution must pass verification before publish |
| Bad merge in branch | Verification step (apply + merge + CI) before push |
| Unbounded fan-out on push to default | `MAX_PARALLEL=4` + sqlite queue absorbs burst |
| Long-running git ops blocking other webhooks | Worker process is separate from existing receiver |
| Force-push churn / duplicate webhooks | Persistent dedupe by 5-tuple, 7-day TTL |
| Large monorepo clone latency | Bare-repo cache + worktree per invocation |
| OpenAI Codex CLI session fragility (currently rate-limited) | Use OpenAI API directly with dedicated key |
| Quota DoS | OpenAI account monthly cap + Claude API spend monitoring |

## Legion task decomposition

The architecture decomposes into 10 independent tasks legion can dispatch in parallel (with declared dependencies):

| Task | Component | Depends on | Effort (CC+legion) |
|---|---|---|---|
| T-001 | GitHub App registration + install-token minting | — | ~30 min |
| T-002 | Webhook receiver + signature verification | T-001 | ~30 min |
| T-003 | sqlite job queue + persistent dedupe | — | ~30 min |
| T-004 | Bare-repo cache + worktree manager | — | ~45 min |
| T-005 | Conflict marker extractor + classifier (heuristic) | — | ~30 min |
| T-006 | OpenAI API client (direct, not CLI) | — | ~20 min |
| T-007 | Claude Code SDK client | — | ~20 min |
| T-008 | Verification harness (apply patch + merge + CI) | T-004 | ~45 min |
| T-009 | Publish: branch push + PR comment with merge link | T-001, T-008 | ~30 min |
| T-010 | End-to-end fixture test (real PR, real conflict, mocked LLM) | T-001 through T-009 | ~45 min |

Estimated wall-clock with `MAX_PARALLEL=4`: ~2 hours from dispatch to all PRs merged.

## Rollout plan

1. Dogfood: install on `Screddyice/pr-conflict-bot` itself; trigger fixture conflict
2. Low-stakes: install on one NEB repo; observe 5+ real conflicts before expanding
3. NEB org-wide: install at org level
4. TRC org: when TRC repos exist (per memory, infra unprovisioned currently)

## Required tests

- **Unit:** classifier rules, signature verifier, worktree manager, dedupe, error handlers
- **Integration:** OpenAI API client (mocked), Claude SDK (mocked), GitHub API (mocked)
- **Fixture e2e:** create PR with known conflict on test repo, run bot end-to-end, assert branch + comment land
- **Failure paths:** mocked timeouts, rate-limits, malformed LLM responses, verify failures, both engines fail

The "sleep at 2am" check: **fixture e2e + verification harness** together guarantee no bad merge gets published.
