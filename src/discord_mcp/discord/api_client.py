"""Authenticated calls to Discord's internal JSON API via the logged-in browser.

Discord's `/api/v9/...` endpoints require an `Authorization` header carrying
the user's session token. The token is stashed inside Discord's webpack runtime
and removed from `window.localStorage` on load — but it is still accessible
through a fresh same-origin iframe, which is our extraction path.

This is explicitly less polite than DOM scraping: it speaks the same protocol
Discord's web client speaks. Use it only where scraping is too fragile (search,
pins, threads, mentions) and accept the higher ban risk documented in README.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

from ..errors import DiscordMcpError


class ApiUnavailable(DiscordMcpError):
    """Raised when we can't authenticate against Discord's internal API."""


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    body: str


class DiscordApiClient:
    """Makes authenticated Discord API calls from a logged-in page context."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._token: str | None = None

    async def _token_once(self) -> str:
        if self._token:
            return self._token
        token = await self._page.evaluate(_TOKEN_EXTRACT_JS)
        if not isinstance(token, str) or not token:
            raise ApiUnavailable(
                "Could not extract Discord auth token from the browser. "
                "This typically means you're not logged in or Discord changed "
                "how it stores the token."
            )
        self._token = token
        return token

    async def fetch_json(
        self, path: str, *, method: str = "GET", body: Any = None
    ) -> Any:
        """Call a Discord API endpoint and return the parsed JSON response.

        Non-2xx responses raise ApiUnavailable with the status + body snippet.
        """
        token = await self._token_once()
        payload: dict[str, Any] = {"path": path, "token": token, "method": method}
        if body is not None:
            payload["body"] = json.dumps(body)
        raw = await self._page.evaluate(_FETCH_JS, payload)
        resp = ApiResponse(status=int(raw["status"]), body=str(raw["body"]))
        if resp.status < 200 or resp.status >= 300:
            raise ApiUnavailable(
                f"Discord API {method} {path} → {resp.status}: {resp.body[:200]}"
            )
        if not resp.body:
            return None
        return json.loads(resp.body)


# Discord removes the `token` key from `window.localStorage` as soon as the
# client boots, so grabbing it directly fails. A same-origin iframe gets its
# own fresh localStorage view (from session storage) where the token is still
# present — that's the pattern this uses.
_TOKEN_EXTRACT_JS = """
() => {
    try {
        const iframe = document.createElement('iframe');
        document.body.appendChild(iframe);
        const raw = iframe.contentWindow && iframe.contentWindow.localStorage
            ? iframe.contentWindow.localStorage.token
            : null;
        iframe.remove();
        return raw ? JSON.parse(raw) : null;
    } catch (e) {
        return null;
    }
}
"""

_FETCH_JS = """
async ({ path, token, method, body }) => {
    const init = {
        method,
        headers: {
            'Authorization': token,
            'Content-Type': 'application/json',
        },
        credentials: 'same-origin',
    };
    if (body !== undefined) init.body = body;
    const resp = await fetch(path, init);
    return { status: resp.status, body: await resp.text() };
}
"""
