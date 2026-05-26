"""Browser engine abstraction for the QA pass.

`BrowseEngine` is the seam between the QA orchestrator and the actual headless
browser. `SubprocessBrowse` drives the vendored gstack `browse` binary;
`FakeBrowse` is the test double. M1 only needs a single-page snapshot.

The gstack `browse` CLI is a stateful session daemon (one persistent headless
Chromium per process cwd / project slug), not a one-shot tool. A page snapshot
is therefore a *sequence* of commands against that daemon:

    browse goto <url>          -> "Navigated to <url> (<http_status>)"
    browse text                -> visible text, wrapped in UNTRUSTED markers
    browse console --errors    -> "[<ts>] [error] <msg>" lines (or "(no console errors)")
    browse screenshot <path>   -> writes a PNG (path sandboxed to cwd / /private/tmp)

The daemon auto-starts on the first command and is meant to persist, so capture
does NOT stop it. Because all commands act on the daemon's single *active* tab,
concurrent captures would interleave; a per-instance lock serializes them.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger()

_NAV_RE = re.compile(r"Navigated to .*?\((\d+)\)")
_BEGIN_RE = re.compile(r"^--- BEGIN UNTRUSTED EXTERNAL CONTENT.*$", re.MULTILINE)
_END_RE = re.compile(r"^--- END UNTRUSTED EXTERNAL CONTENT.*$", re.MULTILINE)
_ERROR_LINE_RE = re.compile(r"\[error\]\s*(.*\S)\s*$")
_NO_ERRORS = "(no console errors)"


@dataclass(frozen=True)
class PageState:
    url: str
    http_status: int
    console_errors: tuple[str, ...]
    text: str
    screenshot_path: Path | None


class BrowseEngine(Protocol):
    async def capture(self, url: str, *, screenshot_to: Path | None = None) -> PageState: ...


class FakeBrowse:
    """Test double: returns a preset PageState and records the URLs it saw."""

    def __init__(self, state: PageState) -> None:
        self._state = state
        self.captured_urls: list[str] = []

    async def capture(self, url: str, *, screenshot_to: Path | None = None) -> PageState:
        self.captured_urls.append(url)
        return self._state


class BrowseError(RuntimeError):
    pass


def parse_nav_status(goto_output: str) -> int:
    """Extract the HTTP status from `goto` output ('Navigated to <url> (200)').

    Returns 0 if no status line is present (e.g. the navigation errored).
    """
    m = _NAV_RE.search(goto_output)
    return int(m.group(1)) if m else 0


def unwrap_untrusted(output: str) -> str:
    """Strip the BEGIN/END UNTRUSTED EXTERNAL CONTENT markers `browse` wraps
    page-derived output in, returning the inner content. Tolerates a missing
    wrapper (returns the input stripped)."""
    begin = _BEGIN_RE.search(output)
    end = _END_RE.search(output)
    if begin and end and end.start() > begin.end():
        return output[begin.end() : end.start()].strip()
    return output.strip()


def parse_console_errors(console_output: str) -> tuple[str, ...]:
    """Parse `console --errors` output into a tuple of error messages.

    Output is UNTRUSTED-wrapped and is either the literal '(no console errors)'
    or one '[<ts>] [error] <msg>' line per error.
    """
    inner = unwrap_untrusted(console_output)
    if not inner or inner == _NO_ERRORS:
        return ()
    errors: list[str] = []
    for line in inner.splitlines():
        m = _ERROR_LINE_RE.search(line)
        if m:
            errors.append(m.group(1))
    return tuple(errors)


class SubprocessBrowse:
    """Drives the vendored gstack `browse` session daemon to snapshot one page.

    `cwd` is the working directory `browse` runs in. It both selects the daemon
    instance (project slug) and bounds the screenshot path sandbox, so it should
    be a writable directory that contains any `screenshot_to` target (the bot's
    work dir). Captures are serialized via `self._lock` because the daemon has a
    single active tab.
    """

    def __init__(self, binary: str, *, cwd: Path | None = None) -> None:
        self._binary = binary
        self._cwd = cwd
        self._lock = asyncio.Lock()

    async def _run_text(self, args: list[str], *, timeout: float) -> str:
        argv = [self._binary, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self._cwd) if self._cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            raise BrowseError(f"could not launch browse ({self._binary!r}): {e}") from e
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise BrowseError(f"browse {args[0]} timed out after {timeout}s") from e
        if proc.returncode != 0:
            raise BrowseError(
                f"browse {args[0]} failed (rc={proc.returncode}): "
                f"{err_b.decode(errors='replace')}"
            )
        return out_b.decode(errors="replace")

    async def capture(
        self, url: str, *, screenshot_to: Path | None = None, timeout: float = 120.0
    ) -> PageState:
        async with self._lock:
            goto_out = await self._run_text(["goto", url], timeout=timeout)
            status = parse_nav_status(goto_out)
            text = unwrap_untrusted(await self._run_text(["text"], timeout=timeout))
            errors = parse_console_errors(
                await self._run_text(["console", "--errors"], timeout=timeout)
            )

            shot: Path | None = None
            if screenshot_to is not None:
                # Best-effort: a screenshot is diagnostic, not load-bearing for the
                # M1 report, and is the most likely step to hit the path sandbox.
                try:
                    await self._run_text(["screenshot", str(screenshot_to)], timeout=timeout)
                    if screenshot_to.exists():
                        shot = screenshot_to
                except BrowseError as e:
                    log.warning("qa screenshot failed", url=url, reason=str(e))

            return PageState(
                url=url,
                http_status=status,
                console_errors=errors,
                text=text,
                screenshot_path=shot,
            )
