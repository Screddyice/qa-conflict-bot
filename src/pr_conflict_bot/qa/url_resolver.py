"""Resolve a live URL for the PR's app.

M1 supports only the per-repo start-command path: run `[qa] build` (optional),
run `[qa] start` as a background process group, poll `[qa] url` until it
responds, yield the URL, then terminate the group on exit. Preview-deploy
detection is a later milestone. Commands come from the repo's own
.pr-conflict-bot.toml (owner-controlled, same trust model as the [verify] gate).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import structlog

from ..config import QAConfig

log = structlog.get_logger()


class URLResolutionError(RuntimeError):
    pass


async def _wait_until_ready(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(url) as resp:
                    if resp.status < 500:
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.5)
    raise URLResolutionError(f"server did not become ready at {url} within {timeout}s")


@asynccontextmanager
async def serve_via_start_command(
    repo_dir: Path, qa: QAConfig, *, ready_timeout: float = 120.0
) -> AsyncIterator[str]:
    if not qa.start or not qa.url:
        raise URLResolutionError(
            "`[qa] start` and `[qa] url` are each required (one or both missing)"
        )

    if qa.build:
        builder = await asyncio.create_subprocess_shell(
            qa.build, cwd=str(repo_dir),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err_b = await builder.communicate()
        if builder.returncode != 0:
            raise URLResolutionError(f"build failed: {err_b.decode(errors='replace')}")

    log.debug("qa.start_command.launching", command=qa.start, cwd=str(repo_dir))
    server = await asyncio.create_subprocess_shell(
        qa.start,
        cwd=str(repo_dir),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,  # own process group so we can kill children too
    )
    try:
        await _wait_until_ready(qa.url, ready_timeout)
        yield qa.url
    finally:
        log.debug("qa.start_command.terminating", pid=server.pid)
        pgid: int | None
        try:
            pgid = os.getpgid(server.pid)
        except ProcessLookupError:
            pgid = None
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(server.wait(), timeout=5.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(server.wait(), timeout=5.0)
        # brief yield so the OS reclaims the port before the caller can rebind
        await asyncio.sleep(0.1)
