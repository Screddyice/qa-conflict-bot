# Contributing

Thanks for your interest. A few ground rules:

## Development setup

```bash
git clone https://github.com/Screddyice/pr-conflict-bot
cd pr-conflict-bot
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Before opening a PR

```bash
ruff check .
mypy src
pytest -q
```

CI runs the same on every PR.

## Scope

This project does one thing: resolve PR merge conflicts and push the fix back. PRs that expand scope (e.g., "also auto-merge", "also generate code from issues") will be politely declined or split.

Things that *are* in scope:

- Improvements to the conflict-resolution prompt or its inputs
- New LLM backends behind the same `resolve_file` interface
- Better verify-gate ergonomics (auto-detecting `package.json`, `pyproject.toml`, etc.)
- Better failure-mode handling (more PR comment detail, retry logic, dead-letter on repeated failures)

## Commit style

Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`). One logical change per commit.

## License

By contributing you agree your contributions are MIT-licensed.
