"""AuthService: orchestrates the interactive login flow.

The MCP server itself never runs a login — it just loads whatever session
the AuthService previously captured. This separation means the MCP server
has zero code paths that type a password or poll a login form.
"""

from __future__ import annotations

import asyncio
import pathlib as pl
from dataclasses import dataclass
from typing import cast

from playwright.async_api import async_playwright

from ..models import SessionData
from .session_store import SessionStore


_LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes for the user to finish 2FA
_LOGGED_IN_SELECTOR = '[data-list-id="guildsnav"] [role="treeitem"]'
_DISCORD_LOGIN_URL = "https://discord.com/login"


@dataclass(frozen=True, slots=True)
class AuthStatus:
    session_path: pl.Path
    is_authenticated: bool


class AuthService:
    """Manages the Discord session lifecycle: login, logout, status."""

    def __init__(self, store: SessionStore, *, session_path: pl.Path) -> None:
        self._store = store
        self._session_path = session_path

    async def login(self) -> pl.Path:
        """Open a headful browser, wait for the user to log in, persist the session.

        Returns the path to the encrypted session file.
        """
        session_data = await self._capture_session()
        self._store.save(session_data)
        return self._session_path

    def logout(self) -> None:
        """Remove both the session file and the keyring key."""
        self._store.delete()

    def status(self) -> AuthStatus:
        return AuthStatus(
            session_path=self._session_path,
            is_authenticated=self._store.exists(),
        )

    @staticmethod
    async def _capture_session() -> SessionData:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False)
            try:
                context = await browser.new_context()
                page = await context.new_page()

                await page.goto(_DISCORD_LOGIN_URL)
                print("Browser opened. Log in to Discord (including 2FA).")
                print("Waiting for login to complete…")

                await page.wait_for_selector(
                    _LOGGED_IN_SELECTOR,
                    state="visible",
                    timeout=_LOGIN_TIMEOUT_MS,
                )
                # Short settle so post-login state (last channel, etc.) lands in
                # localStorage before we snapshot.
                await asyncio.sleep(2)

                # Playwright's StorageState TypedDict is a valid SessionData
                # (dict[str, Any]); cast at the infra boundary.
                return cast(SessionData, await context.storage_state())
            finally:
                await browser.close()
