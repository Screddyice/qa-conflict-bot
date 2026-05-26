"""Minimal Linear API client for posting QA findings to a PR's Linear issue.

The bot is org-agnostic. It holds a per-owner Linear token map (`LINEAR_TOKENS`
env, a server-side secret — never committed) and resolves the issue for a PR via
Linear's native GitHub attachment (`attachmentsForURL`). It does NOT create
tickets and does not care how they were created (Linear's GitHub integration or
a separate webhook handler both produce the same PR attachment). Best-effort:
callers wrap failures so Linear can never break the QA flow.
"""

from __future__ import annotations

import aiohttp

LINEAR_GRAPHQL = "https://api.linear.app/graphql"


class LinearError(RuntimeError):
    pass


class LinearClient:
    def __init__(self, token: str, session: aiohttp.ClientSession) -> None:
        self._token = token
        self._session = session

    async def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        # Linear personal API keys go in Authorization with no "Bearer" prefix.
        headers = {"Authorization": self._token, "Content-Type": "application/json"}
        async with self._session.post(
            LINEAR_GRAPHQL, headers=headers, json={"query": query, "variables": variables}
        ) as r:
            if r.status != 200:
                raise LinearError(f"linear graphql failed ({r.status}): {await r.text()}")
            payload: dict[str, object] = await r.json()
        if payload.get("errors"):
            raise LinearError(f"linear graphql errors: {payload['errors']}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LinearError("linear graphql response missing 'data'")
        return data

    async def find_issue_id_by_url(self, url: str) -> str | None:
        """Return the Linear issue id whose GitHub attachment matches `url`, else None.

        Linear's GitHub integration attaches a PR to its issue, so the PR's html
        URL resolves back to the issue. Returns None when nothing is linked yet
        (e.g. the attachment hasn't landed, or this repo has no Linear at all).
        """
        query = "query($url: String!) { attachmentsForURL(url: $url) { nodes { issue { id } } } }"
        data = await self._graphql(query, {"url": url})
        attachments = data.get("attachmentsForURL")
        if not isinstance(attachments, dict):
            return None
        nodes = attachments.get("nodes")
        if not isinstance(nodes, list):
            return None
        for node in nodes:
            if isinstance(node, dict):
                issue = node.get("issue")
                if isinstance(issue, dict) and issue.get("id"):
                    return str(issue["id"])
        return None

    async def comment(self, issue_id: str, body: str) -> None:
        mutation = (
            "mutation($issueId: String!, $body: String!) "
            "{ commentCreate(input: {issueId: $issueId, body: $body}) { success } }"
        )
        data = await self._graphql(mutation, {"issueId": issue_id, "body": body})
        result = data.get("commentCreate")
        if not isinstance(result, dict) or not result.get("success"):
            raise LinearError(f"linear commentCreate did not succeed: {result}")
