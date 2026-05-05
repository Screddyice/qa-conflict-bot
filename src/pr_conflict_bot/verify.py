"""Verify gate: lint + typecheck + test on the resolved tree.

Runs configured commands; collects logs; returns pass/fail. Never raises on command
failure — failure is a normal outcome the orchestrator must report on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .config import VerifyConfig


@dataclass(frozen=True)
class StepResult:
    name: str
    command: str
    passed: bool
    skipped: bool
    output: str  # combined stdout+stderr, truncated


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    steps: tuple[StepResult, ...]

    def summary(self) -> str:
        lines = []
        for s in self.steps:
            mark = "skip" if s.skipped else ("pass" if s.passed else "FAIL")
            lines.append(f"[{mark}] {s.name}: {s.command or '(unset)'}")
        return "\n".join(lines)


_OUTPUT_LIMIT = 8000  # chars per step in summary; full logs go to structured logger


async def _run_step(name: str, command: str, cwd: Path, timeout: float) -> StepResult:
    if not command.strip():
        return StepResult(name=name, command="", passed=True, skipped=True, output="")

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return StepResult(
            name=name,
            command=command,
            passed=False,
            skipped=False,
            output=f"(timed out after {timeout}s)",
        )

    out = out_b.decode(errors="replace")
    if len(out) > _OUTPUT_LIMIT:
        out = out[: _OUTPUT_LIMIT // 2] + "\n... [truncated] ...\n" + out[-_OUTPUT_LIMIT // 2 :]

    return StepResult(
        name=name,
        command=command,
        passed=proc.returncode == 0,
        skipped=False,
        output=out,
    )


async def run(cfg: VerifyConfig, repo_dir: Path) -> VerifyResult:
    steps = []
    for name, cmd in (("lint", cfg.lint), ("typecheck", cfg.typecheck), ("test", cfg.test)):
        steps.append(await _run_step(name, cmd, repo_dir, cfg.timeout_seconds))
    passed = all(s.passed for s in steps)
    return VerifyResult(passed=passed, steps=tuple(steps))
