"""Browser engine abstraction for the QA pass.

`BrowseEngine` is the seam between the QA orchestrator and the actual headless
browser. `SubprocessBrowse` drives the vendored gstack `browse` binary;
`FakeBrowse` is the test double. M1 only needs a single-page snapshot.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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


class SubprocessBrowse:
    """Drives the vendored `browse` binary, which emits a JSON page snapshot.

    Command contract (validated by the M1 smoke task once browse is vendored):
        browse snapshot <url> [--screenshot <path>] --json
    emitting {"http_status": int, "console_errors": [str], "text": str}.
    """

    def __init__(self, binary: str) -> None:
        self._binary = binary

    async def _run_json(self, cmd: list[str], *, timeout: float) -> dict[str, object]:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise BrowseError(f"browse timed out after {timeout}s") from e
        if proc.returncode != 0:
            raise BrowseError(
                f"browse failed (rc={proc.returncode}): {err_b.decode(errors='replace')}"
            )
        parsed: dict[str, object] = json.loads(out_b.decode(errors="replace"))
        return parsed

    async def capture(
        self, url: str, *, screenshot_to: Path | None = None, timeout: float = 120.0
    ) -> PageState:
        cmd = [self._binary, "snapshot", url, "--json"]
        if screenshot_to is not None:
            cmd += ["--screenshot", str(screenshot_to)]
        data = await self._run_json(cmd, timeout=timeout)
        raw_errors = data.get("console_errors") or []
        errors = tuple(str(e) for e in raw_errors) if isinstance(raw_errors, list) else ()
        return PageState(
            url=url,
            http_status=int(data.get("http_status", 0)),  # type: ignore[call-overload]
            console_errors=errors,
            text=str(data.get("text", "")),
            screenshot_path=screenshot_to,
        )
