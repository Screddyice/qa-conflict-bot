from __future__ import annotations

import socket
from pathlib import Path

import aiohttp
import pytest

from pr_conflict_bot.config import QAConfig
from pr_conflict_bot.qa.url_resolver import URLResolutionError, serve_via_start_command


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_serves_and_tears_down(tmp_path: Path) -> None:
    port = _free_port()
    (tmp_path / "index.html").write_text("<h1>hello</h1>")
    qa = QAConfig(
        enabled=True,
        url=f"http://127.0.0.1:{port}",
        start=f"python3 -m http.server {port}",
    )
    async with serve_via_start_command(tmp_path, qa, ready_timeout=15.0) as url:
        assert url == qa.url
        async with aiohttp.ClientSession() as s, s.get(url) as resp:
            assert resp.status == 200
    # after exit the port should be free again (server killed)
    s2 = socket.socket()
    s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s2.bind(("127.0.0.1", port))  # would raise if still bound (SO_REUSEADDR avoids TIME_WAIT on macOS)
    s2.close()


async def test_missing_start_or_url_raises(tmp_path: Path) -> None:
    with pytest.raises(URLResolutionError):
        async with serve_via_start_command(tmp_path, QAConfig(enabled=True)) as _:
            pass
