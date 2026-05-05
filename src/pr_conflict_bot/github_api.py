"""GitHub App authentication and a minimal REST client for the operations we need.

Auth flow:
    JWT (signed with the App's private key) → GET /repos/{owner}/{repo}/installation
    → POST /app/installations/{id}/access_tokens → installation token used as Bearer.
Tokens are short-lived; we cache by installation_id with a small safety margin.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC
from typing import Any, cast

import aiohttp
import jwt

from .config import GitHubAppConfig

GITHUB_API = "https://api.github.com"
_TOKEN_REFRESH_SAFETY_SECONDS = 60


class GitHubError(RuntimeError):
    pass


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class GitHubClient:
    def __init__(self, app_cfg: GitHubAppConfig, session: aiohttp.ClientSession):
        self._cfg = app_cfg
        self._session = session
        self._tokens: dict[int, _CachedToken] = {}

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + 540, "iss": self._cfg.app_id}
        return jwt.encode(payload, self._cfg.private_key_pem, algorithm="RS256")

    async def _installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached.expires_at - _TOKEN_REFRESH_SAFETY_SECONDS > time.time():
            return cached.token

        url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with self._session.post(url, headers=headers) as r:
            if r.status not in (200, 201):
                body = await r.text()
                raise GitHubError(f"installation token request failed ({r.status}): {body}")
            data = await r.json()

        # Format: 2024-01-01T00:00:00Z
        from datetime import datetime
        expires_at = datetime.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        ).timestamp()
        token = cast(str, data["token"])
        self._tokens[installation_id] = _CachedToken(token=token, expires_at=expires_at)
        return token

    async def clone_url(self, installation_id: int, owner: str, repo: str) -> str:
        token = await self._installation_token(installation_id)
        return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"

    async def get_pr(
        self, installation_id: int, owner: str, repo: str, pr_number: int
    ) -> dict[str, Any]:
        token = await self._installation_token(installation_id)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with self._session.get(url, headers=headers) as r:
            if r.status != 200:
                raise GitHubError(f"get_pr failed ({r.status}): {await r.text()}")
            return cast(dict[str, Any], await r.json())

    async def post_issue_comment(
        self, installation_id: int, owner: str, repo: str, pr_number: int, body: str
    ) -> None:
        token = await self._installation_token(installation_id)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with self._session.post(url, headers=headers, json={"body": body}) as r:
            if r.status not in (200, 201):
                raise GitHubError(f"comment failed ({r.status}): {await r.text()}")

    async def dismiss_self_reviews(
        self, installation_id: int, owner: str, repo: str, pr_number: int
    ) -> None:
        """Dismiss the bot's prior reviews on this PR (if any)."""
        token = await self._installation_token(installation_id)
        list_url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with self._session.get(list_url, headers=headers) as r:
            if r.status != 200:
                raise GitHubError(f"list reviews failed ({r.status}): {await r.text()}")
            reviews = await r.json()

        for rev in reviews:
            if rev.get("user", {}).get("login") != self._cfg.bot_login:
                continue
            if rev.get("state") in ("DISMISSED", "PENDING"):
                continue
            rev_id = rev["id"]
            dismiss_url = (
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
                f"/reviews/{rev_id}/dismissals"
            )
            async with self._session.put(
                dismiss_url, headers=headers, json={"message": "Superseded by new commit."}
            ) as r:
                if r.status not in (200, 201):
                    raise GitHubError(
                        f"dismiss review {rev_id} failed ({r.status}): {await r.text()}"
                    )
