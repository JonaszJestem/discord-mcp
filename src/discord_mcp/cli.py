"""CLI entry point and composition root.

Subcommands:
    discord-mcp serve   — run the MCP stdio server (what MCP clients invoke)
    discord-mcp login   — open a browser for interactive Discord login
    discord-mcp logout  — clear the stored session
    discord-mcp status  — show whether a session is stored

All wiring of concrete implementations lives here.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable

from .auth import AuthService, EncryptedFileSessionStore, OSKeyringVault, SessionStore
from .cache import DiscoveryCache
from .config import Config
from .discord import BrowserPool, DiscordBrowserDriver, DiscordService
from .errors import DiscordMcpError
from .mcp_server import create_mcp_server


def main() -> int:
    try:
        return _dispatch(sys.argv[1:])
    except DiscordMcpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------- dispatch


def _dispatch(args: list[str]) -> int:
    commands: dict[str, Callable[[], int]] = {
        "serve": _cmd_serve,
        "login": _cmd_login,
        "logout": _cmd_logout,
        "status": _cmd_status,
    }
    if not args:
        return _usage()
    handler = commands.get(args[0])
    if handler is None:
        return _usage()
    return handler()


def _usage() -> int:
    print("usage: discord-mcp {serve | login | logout | status}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------- commands


def _cmd_serve() -> int:
    config = Config.load()
    store = _build_session_store(config)
    session = store.load()  # surfaces SessionMissing / SessionCorrupt at startup

    async def driver_factory() -> DiscordBrowserDriver:
        return await DiscordBrowserDriver.create(session, headless=config.headless)

    pool = BrowserPool(factory=driver_factory, max_size=config.pool_size)
    service = DiscordService(pool, DiscoveryCache(config.cache_path))
    create_mcp_server(service, config).run()
    return 0


def _cmd_login() -> int:
    config = Config.load()
    store = _build_session_store(config)
    service = AuthService(store, session_path=config.session_path)

    try:
        path = asyncio.run(service.login())
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        return 1
    print(f"Login saved to {path} (encrypted).")
    return 0


def _cmd_logout() -> int:
    config = Config.load()
    store = _build_session_store(config)
    AuthService(store, session_path=config.session_path).logout()
    print("Session cleared.")
    return 0


def _cmd_status() -> int:
    config = Config.load()
    store = _build_session_store(config)
    service = AuthService(store, session_path=config.session_path)
    status = service.status()
    print(f"Session file: {status.session_path}")
    if status.is_authenticated:
        print("Logged in.")
        return 0
    print("Not logged in. Run `discord-mcp login`.")
    return 1


# ---------------------------------------------------------------- wiring


def _build_session_store(config: Config) -> SessionStore:
    vault = OSKeyringVault(service=config.keyring_service, user=config.keyring_user)
    return EncryptedFileSessionStore(path=config.session_path, vault=vault)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
