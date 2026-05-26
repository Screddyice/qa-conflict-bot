from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pr_conflict_bot.config import (
    BotIdentity,
    Config,
    GitHubAppConfig,
    LLMConfig,
    VerifyConfig,
)
from pr_conflict_bot.server import PRJob, make_app


def _cfg() -> Config:
    return Config(
        github=GitHubAppConfig(app_id=1, private_key_pem="x", webhook_secret="secret", bot_login="b[bot]"),
        llm=LLMConfig(), verify=VerifyConfig(), identity=BotIdentity(),
        listen_host="127.0.0.1", listen_port=8081, webhook_path="/hooks/github",
        work_dir=Path("/tmp"), log_level="INFO",
    )


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload() -> dict[str, object]:
    return {
        "action": "opened",
        "pull_request": {
            "number": 5, "draft": False, "state": "open",
            "head": {"ref": "feat", "sha": "deadbeef"}, "base": {"ref": "main"},
            "user": {"login": "alice"},
        },
        "repository": {"full_name": "o/r", "owner": {"login": "o"}},
        "installation": {"id": 99},
        "sender": {"login": "alice", "type": "User"},
    }


async def test_enqueues_to_both_queues() -> None:
    cfg = _cfg()
    conflict_q: asyncio.Queue[PRJob] = asyncio.Queue()
    qa_q: asyncio.Queue[PRJob] = asyncio.Queue()
    app = make_app(cfg, conflict_q, qa_queue=qa_q)

    async with TestClient(TestServer(app)) as client:
        body = json.dumps(_payload()).encode()
        resp = await client.post(
            "/hooks/github",
            data=body,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d1",
                "X-Hub-Signature-256": _sign("secret", body),
            },
        )
        assert resp.status == 202

    assert conflict_q.qsize() == 1
    assert qa_q.qsize() == 1
    assert qa_q.get_nowait().pr_number == 5
