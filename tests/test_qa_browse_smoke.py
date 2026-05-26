from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from pr_conflict_bot.qa.browse import SubprocessBrowse

_BROWSE = shutil.which("browse")

pytestmark = pytest.mark.skipif(_BROWSE is None, reason="browse binary not installed on this host")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_real_browse_captures_a_page(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<h1 id='t'>Hello QA</h1>")
    port = _free_port()
    server = subprocess.Popen(
        ["python3", "-m", "http.server", str(port)], cwd=tmp_path,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)
        assert _BROWSE is not None
        sb = SubprocessBrowse(binary=_BROWSE)
        state = await sb.capture(f"http://127.0.0.1:{port}")
        assert state.http_status == 200
        assert "Hello QA" in state.text
    finally:
        server.terminate()
        server.wait(timeout=5)
