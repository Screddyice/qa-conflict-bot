"""Wrapper around `codex exec` for resolving a single conflicted file.

The bot, not codex, drives git. Codex is scoped to: given diffs and a conflicted file,
write the resolved content back to disk. We then verify no markers remain and re-read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .config import CodexConfig


class CodexError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolveRequest:
    repo_dir: Path
    file_path: str
    head_diff: str       # PR's intent: git diff --merge-base <base> HEAD -- <file>
    base_diff: str       # main's intent: git diff --merge-base HEAD <base> -- <file>
    conflicted_content: str  # current on-disk content with zdiff3 markers


PROMPT_TEMPLATE = """\
You are resolving a Git merge conflict in a single file. The file is at:

    {file_path}

It currently contains zdiff3-style conflict markers (`<<<<<<<`, `|||||||` for the merge base,
`=======`, `>>>>>>>`).

Below is the semantic context:

=== PR's intent on this file (diff from merge base to PR HEAD) ===
{head_diff}

=== Base branch's intent on this file (diff from merge base to base HEAD) ===
{base_diff}

=== Current file with conflict markers ===
{conflicted_content}

Resolve the conflict by writing the merged file content back to {file_path}. Preserve
both sides' semantic intent where they don't actually conflict; when they do conflict,
prefer the change that keeps the codebase consistent and tests passing. Do NOT add
explanatory comments about the merge. Do NOT leave any conflict markers. Do NOT modify
any file other than {file_path}.

When you are done, the file at {file_path} must contain valid source code with no
remaining conflict markers.
"""


async def resolve_file(req: ResolveRequest, cfg: CodexConfig, *, timeout: float = 300.0) -> None:
    """Invoke codex to resolve req.file_path in place. Raises CodexError on failure."""
    prompt = PROMPT_TEMPLATE.format(
        file_path=req.file_path,
        head_diff=req.head_diff or "(no PR-side changes — file was unchanged on PR branch)",
        base_diff=req.base_diff or "(no base-side changes — file was unchanged on base)",
        conflicted_content=req.conflicted_content,
    )

    cmd = [
        cfg.binary,
        "exec",
        "--full-auto",
        "--sandbox", cfg.sandbox,
        "--cd", str(req.repo_dir),
        *cfg.extra_args,
        prompt,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise CodexError(f"codex exec timed out after {timeout}s") from e

    if proc.returncode != 0:
        raise CodexError(
            f"codex exec failed (rc={proc.returncode}):\n"
            f"stdout:\n{stdout_b.decode(errors='replace')}\n"
            f"stderr:\n{stderr_b.decode(errors='replace')}"
        )
