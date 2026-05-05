# pr-conflict-bot

A GitHub bot that resolves merge conflicts on open pull requests using a code-aware LLM, then pushes the resolution back to the PR branch — so the human author can review and merge.

**It does not auto-merge. It only makes conflicted PRs mergeable.**

Two LLM backends supported, both billed against the model vendor's subscription (no per-token API charges):

- **`claude`** (default) — [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) headless mode (`claude -p`). Auth via `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, runs against your Claude Pro/Max subscription.
- **`codex`** — [OpenAI Codex CLI](https://github.com/openai/codex) (`codex exec`). Auth via `codex login` on the host, runs against your ChatGPT Plus/Pro subscription.

Set `LLM_BACKEND` in the env file to pick.

## What it does

For every `pull_request` `opened` / `synchronize` / `reopened` event in a repo where the App is installed:

1. Clone the PR head, fetch the base branch.
2. `git merge origin/<base>` — if it's clean, exit (nothing to do).
3. For each conflicted file, hand the LLM:
   - PR-side intent: `git diff --merge-base <base> HEAD -- <file>`
   - Base-side intent: `git diff --merge-base HEAD <base> -- <file>`
   - The conflicted file with `merge.conflictStyle = zdiff3` markers (merge base visible)
4. Verify the resolution: run `lint && typecheck && test` (configurable per-repo).
5. **If verify passes**, `git push --force-with-lease=<head_sha>` back to the PR branch.
6. Comment on the PR with what was resolved and what verify found.

If anything fails — codex left markers, verify failed, lock file conflict, race with the human pushing — the bot bails and posts an explanatory comment. **It never pushes an unverified resolution.**

## Why these choices

- **Merge, not rebase, for conflict detection.** Rebase replays N commits and surfaces the same conflict N times against different intermediate trees; that's bad context for an LLM. Merge gives one conflict against one base.
- **`zdiff3` conflict style.** Surfaces the merge base inside the markers, which the LLM uses to tell "both sides changed the same thing differently" from "one side changed, other side didn't touch it."
- **Three diffs, not just the marker file.** The marker file alone is textual; the merge-base diffs are *semantic*. Resolutions get noticeably better.
- **`--force-with-lease=<sha>`, never bare `--force`.** Protects against the human author pushing concurrently.
- **Verify gate.** Mandatory. Lint + typecheck + test must pass on the resolved tree before any push.
- **Bot does not count toward required reviewers.** Branch protection should require ≥1 *human* reviewer. The bot is a janitor, not an approver.

## Quickstart

### 1. Create a GitHub App

The bot authenticates as a GitHub App. You need: an App ID, a slug, a webhook
secret, and an RSA private key (PEM). GitHub doesn't expose App creation via
plain REST, only via the *manifest flow* — which requires one click in a
browser. The included helper script automates everything around that click:

```bash
python3 scripts/setup-github-app.py \
    --webhook-url 'https://your-host.example.com/hooks/github' \
    --name 'pr-conflict-bot' \
    --output ~/.pr-conflict-bot/setup
```

The script:
1. Starts a local listener on `http://localhost:8765`.
2. Opens your browser to a page that auto-submits the manifest to GitHub.
3. You click **"Create GitHub App"** on GitHub's confirmation page.
4. GitHub redirects back to the local listener with a one-time code.
5. The script exchanges the code for App credentials and writes them to your
   chosen output directory (mode 0600):

   - `app-id` — numeric App ID → `GITHUB_APP_ID`
   - `app-slug` — slug of the App → `GITHUB_BOT_LOGIN=<slug>[bot]`
   - `webhook-secret` → `GITHUB_WEBHOOK_SECRET`
   - `private-key.pem` → SCP this to the server, point `GITHUB_APP_PRIVATE_KEY` at it

6. Prints the install URL. Visit it and install the App on the orgs/repos you
   want covered.

**Permissions configured by the manifest:**
- Repository → Contents: **Read & write**
- Repository → Pull requests: **Read & write**
- Repository → Metadata: **Read**
- Repository → Checks: **Read**

**Events subscribed:** *Pull request*

**Manual alternative:** if you'd rather do it by hand, go to
https://github.com/settings/apps/new and fill in the same permissions and
events. The script just does the form-filling for you.

### 2. Install the bot

Requires Python 3.11+ and one of:
- `claude` CLI on the host (with `CLAUDE_CODE_OAUTH_TOKEN` in env), **or**
- `codex` CLI on the host (already authenticated via `codex login`).

```bash
git clone https://github.com/Screddyice/pr-conflict-bot
cd pr-conflict-bot
pip install -e .
cp examples/.env.example /etc/pr-conflict-bot/env
chmod 600 /etc/pr-conflict-bot/env
# Edit /etc/pr-conflict-bot/env and fill in your App credentials.
```

### 3. Run

For a quick smoke test:

```bash
set -a; source /etc/pr-conflict-bot/env; set +a
pr-conflict-bot
```

For production, install the systemd unit:

```bash
sudo useradd -r -s /usr/sbin/nologin -m -d /var/lib/pr-conflict-bot prbot
sudo mkdir -p /var/lib/pr-conflict-bot/work && sudo chown -R prbot:prbot /var/lib/pr-conflict-bot
sudo cp systemd/pr-conflict-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now pr-conflict-bot
```

The service listens on `127.0.0.1:8081` by default. Front it with TLS (Tailscale Funnel, Caddy, nginx — your choice).

## Strict mode (recommended)

Set `REQUIRE_REPO_CONFIG=true` in the env file. With strict mode on, the bot refuses to attempt resolution unless the effective verify gate has at least one non-empty step (lint, typecheck, or test) — either via env defaults or a per-repo `.pr-conflict-bot.toml`. Without strict mode, an empty verify gate trivially "passes" and the bot would push unverified resolutions.

When strict mode aborts, it leaves a comment explaining what's missing. Repos opt out cleanly via `[behavior] enabled = false` in their override file.

## Per-repo overrides

Drop `.pr-conflict-bot.toml` at a repo root to override defaults. See `examples/.pr-conflict-bot.toml.example`.

```toml
[verify]
test = "pytest -q"
typecheck = "mypy ."

[behavior]
skip_paths = ["package-lock.json", "**/migrations/*"]
max_files_per_pr = 50
```

## Recommended branch protection

For each protected branch (typically `main`):

- ✅ **Require a pull request before merging**
- ✅ **Require approvals: 1** — the App's review does not count
- ✅ **Require status checks to pass** — include your CI checks
- ✅ **Require branches to be up to date before merging** (this is what triggers the bot's job)
- ❌ **Do not** add the bot as a "required reviewer"

## Architecture

```
┌──────────────┐    pull_request event   ┌────────────────┐
│  GitHub      │ ───────────────────────►│ webhook server │
│  (the App)   │                         │ (HMAC verify)  │
└──────────────┘                         └────────┬───────┘
                                                  │ enqueue
                                                  ▼
                                         ┌────────────────┐
                                         │  in-mem queue  │
                                         └────────┬───────┘
                                                  │
                                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│  worker:                                                        │
│   1. clone --branch <pr_branch>                                 │
│   2. git merge origin/<base>                                    │
│   3. for each conflicted file:                                  │
│        claude -p  (or codex exec)                               │
│        with prompt = head_diff + base_diff + conflicted_content │
│   4. lint && typecheck && test                                  │
│   5. git push --force-with-lease                                │
│   6. POST PR comment with summary                               │
└─────────────────────────────────────────────────────────────────┘
```

`src/pr_conflict_bot/` modules:

| File | Role |
|------|------|
| `server.py` | aiohttp webhook receiver; HMAC verify; enqueue |
| `orchestrator.py` | The flow above. Owns git. |
| `git_ops.py` | clone, merge, diff helpers, push-with-lease |
| `llm.py` | Subprocess wrappers for `claude -p` and `codex exec` (selected via `LLM_BACKEND`) |
| `verify.py` | Lint / typecheck / test runner |
| `github_api.py` | App auth (JWT → installation token), comments, dismiss reviews |
| `config.py` | Env-driven config + per-repo TOML overrides |

## Failure modes

| Risk | Mitigation |
|------|-----------|
| Bad LLM resolution lands | Verify gate (lint+typecheck+test) is mandatory. Human review still required by branch protection. |
| Force-push race with human | `--force-with-lease=<sha>` aborts if remote moved |
| Bot push triggers another bot run | `sender.type == "Bot"` filter on the webhook |
| Two PRs both rebase, second now conflicts | Bot re-runs on next webhook; no global serializer needed (only the PR branch is touched) |
| Lock-file conflict that an LLM should not touch | `skip_paths` in `.pr-conflict-bot.toml` |
| Codex hallucinates files outside the conflicted one | `workspace-write` sandbox + post-merge `git diff` validates only conflicted files changed (TODO: enforce in code) |

## Status

Alpha. Not yet recommended for high-stakes branches. The verify gate is the safety net — keep it strict.

## Contributing

PRs welcome. Run `ruff check .`, `mypy src`, and `pytest` before submitting.

## License

MIT — see [LICENSE](LICENSE).
