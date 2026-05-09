"""aiohttp webhook server with HMAC verification and an in-memory work queue."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

import aiohttp
import structlog
from aiohttp import web

from .config import Config
from .github_api import GitHubClient

log = structlog.get_logger()


# Hard-coded org denylist. The bot must never act on PRs in these orgs, even
# if the App is mistakenly installed there or ALLOW_ORGS is misconfigured. The
# Cliqk platform (`mycliqk` GitHub org) is hands-off — see the operator's
# `no_cliqk_actions` rule. Checked before the allowlist so env cannot override.
DENY_ORGS: frozenset[str] = frozenset({"mycliqk", "cliqk"})


@dataclass(frozen=True)
class PRJob:
    delivery_id: str
    installation_id: int
    owner: str
    repo: str
    pr_number: int
    pr_branch: str
    base_branch: str
    pr_head_sha: str
    sender_login: str
    sender_type: str  # "User" | "Bot"


# A handler signature: takes a PRJob and a GitHubClient, runs to completion.
# The orchestrator wires this in; the server only enqueues.
JobHandler = "object"  # callable, kept loose to avoid circular imports


def _verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _should_handle(
    payload: dict[str, Any], cfg: Config, sender_login: str, sender_type: str
) -> str | None:
    """Return None to handle, or a string reason to skip (for logging)."""
    action = payload.get("action")
    if action not in ("opened", "synchronize", "reopened"):
        return f"action={action} ignored"

    pr = payload.get("pull_request") or {}
    if pr.get("draft"):
        return "PR is draft"
    if pr.get("state") != "open":
        return f"PR state={pr.get('state')}"

    org = (payload.get("repository", {}).get("owner", {}).get("login") or "").lower()
    if org in DENY_ORGS:
        return f"org {org!r} is in DENY_ORGS (hands-off)"
    if cfg.allow_orgs and org not in cfg.allow_orgs:
        return f"org {org!r} not in ALLOW_ORGS"

    pr_author = (pr.get("user", {}).get("login") or "").lower()
    if cfg.allow_users and pr_author not in cfg.allow_users:
        return f"PR author {pr_author!r} not in ALLOW_USERS"

    # Push-loop guard: skip if this event was triggered by ourselves.
    if sender_type == "Bot" and sender_login == cfg.github.bot_login:
        return "self-triggered (bot push)"

    return None


def make_app(cfg: Config, queue: asyncio.Queue[PRJob]) -> web.Application:
    app = web.Application()
    app["cfg"] = cfg
    app["queue"] = queue

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def github(req: web.Request) -> web.Response:
        body = await req.read()
        sig = req.headers.get("X-Hub-Signature-256")
        if not _verify_signature(cfg.github.webhook_secret, body, sig):
            return web.Response(status=401, text="bad signature")

        event = req.headers.get("X-GitHub-Event", "")
        delivery = req.headers.get("X-GitHub-Delivery", "?")
        if event != "pull_request":
            return web.Response(status=204)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="bad json")

        sender = payload.get("sender") or {}
        sender_login = sender.get("login", "")
        sender_type = sender.get("type", "User")

        skip_reason = _should_handle(payload, cfg, sender_login, sender_type)
        if skip_reason:
            log.info("skip", delivery=delivery, reason=skip_reason)
            return web.Response(status=204)

        repo_full = payload["repository"]["full_name"]
        owner, repo = repo_full.split("/", 1)
        pr = payload["pull_request"]
        installation_id = (payload.get("installation") or {}).get("id")
        if installation_id is None:
            return web.Response(status=400, text="no installation id")

        job = PRJob(
            delivery_id=delivery,
            installation_id=int(installation_id),
            owner=owner,
            repo=repo,
            pr_number=int(pr["number"]),
            pr_branch=pr["head"]["ref"],
            base_branch=pr["base"]["ref"],
            pr_head_sha=pr["head"]["sha"],
            sender_login=sender_login,
            sender_type=sender_type,
        )
        await queue.put(job)
        log.info(
            "enqueued",
            delivery=delivery,
            owner=owner,
            repo=repo,
            pr=job.pr_number,
            head=job.pr_head_sha[:8],
        )
        return web.Response(status=202)

    app.router.add_get("/health", health)
    app.router.add_post(cfg.webhook_path, github)
    return app


async def serve(cfg: Config, app: web.Application) -> None:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.listen_host, cfg.listen_port)
    await site.start()
    log.info("listening", host=cfg.listen_host, port=cfg.listen_port)


async def with_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))


__all__ = ["GitHubClient", "PRJob", "make_app", "serve", "with_session"]
