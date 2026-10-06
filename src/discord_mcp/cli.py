"""CLI entry point and composition root.

Subcommands:
    discord-mcp serve   — run the MCP stdio server (what MCP clients invoke)
    discord-mcp login   — open a browser for interactive Discord login
    discord-mcp logout  — clear the stored session
    discord-mcp status  — show whether a session is stored
    discord-mcp fetch   — one-shot read, JSON on stdout (for scripts/pollers)

All wiring of concrete implementations lives here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable

from .auth import AuthService, EncryptedFileSessionStore, OSKeyringVault, SessionStore
from .cache import DiscoveryCache
from .config import Config
from .discord import BrowserPool, DiscordBrowserDriver, DiscordService
from .errors import DiscordMcpError
from .mcp_server import create_mcp_server
from .models import snowflake
from .serialization import (
    JsonDict,
    channel_to_dict,
    guild_to_dict,
    message_to_dict,
    thread_to_dict,
)


def main() -> int:
    try:
        return _dispatch(sys.argv[1:])
    except DiscordMcpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------- dispatch


def _dispatch(args: list[str]) -> int:
    commands: dict[str, Callable[[list[str]], int]] = {
        "serve": _cmd_serve,
        "login": _cmd_login,
        "logout": _cmd_logout,
        "status": _cmd_status,
        "fetch": _cmd_fetch,
    }
    if not args:
        return _usage()
    handler = commands.get(args[0])
    if handler is None:
        return _usage()
    return handler(args[1:])


def _usage() -> int:
    print(
        "usage: discord-mcp {serve | login | logout | status | fetch}", file=sys.stderr
    )
    return 2


# ---------------------------------------------------------------- commands


def _cmd_serve(_: list[str]) -> int:
    config = Config.load()
    store = _build_session_store(config)
    session = store.load()  # surfaces SessionMissing / SessionCorrupt at startup

    async def driver_factory() -> DiscordBrowserDriver:
        return await DiscordBrowserDriver.create(session, headless=config.headless)

    pool = BrowserPool(factory=driver_factory, max_size=config.pool_size)
    service = DiscordService(pool, DiscoveryCache(config.cache_path))
    create_mcp_server(service, config).run()
    return 0


def _cmd_login(_: list[str]) -> int:
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


def _cmd_logout(_: list[str]) -> int:
    config = Config.load()
    store = _build_session_store(config)
    AuthService(store, session_path=config.session_path).logout()
    print("Session cleared.")
    return 0


def _cmd_status(_: list[str]) -> int:
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


def _cmd_fetch(argv: list[str]) -> int:
    ns = _fetch_parser().parse_args(argv)
    config = Config.load()
    session = _build_session_store(config).load()

    async def run() -> JsonDict:
        async def driver_factory() -> DiscordBrowserDriver:
            return await DiscordBrowserDriver.create(session, headless=config.headless)

        service = DiscordService(
            BrowserPool(factory=driver_factory, max_size=1),
            DiscoveryCache(config.cache_path),
        )
        try:
            return await _fetch(service, ns)
        finally:
            await service.close()

    json.dump(asyncio.run(run()), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def _fetch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discord-mcp fetch")
    kinds = parser.add_subparsers(dest="kind", required=True)

    kinds.add_parser("guilds", help="servers the session can see")

    channels = kinds.add_parser("channels", help="channels in a server")
    channels.add_argument("--guild", required=True)

    mentions = kinds.add_parser("mentions", help="the session owner's @-mentions")
    mentions.add_argument("--limit", type=int, default=50)

    threads = kinds.add_parser("threads", help="threads under a channel")
    threads.add_argument("--guild", required=True)
    threads.add_argument("--channel", required=True)

    for kind, target in (("channel", "--channel"), ("thread", "--thread")):
        reader = kinds.add_parser(kind, help=f"recent messages in a {kind}")
        reader.add_argument("--guild", required=True)
        reader.add_argument(target, required=True, dest="target")
        reader.add_argument("--hours", type=int, default=24)
        reader.add_argument("--limit", type=int, default=200)

    return parser


async def _fetch(service: DiscordService, ns: argparse.Namespace) -> JsonDict:
    kind = str(ns.kind)

    if kind == "guilds":
        guilds = await service.list_guilds()
        return _payload(kind, [guild_to_dict(g) for g in guilds])

    if kind == "mentions":
        mentions = await service.list_mentions(limit=int(ns.limit))
        return _payload(kind, [message_to_dict(m) for m in mentions])

    guild = snowflake(str(ns.guild), field="guild")

    if kind == "channels":
        channels = await service.list_channels(guild)
        return _payload(kind, [channel_to_dict(c) for c in channels])

    if kind == "threads":
        channel = snowflake(str(ns.channel), field="channel")
        threads = await service.list_threads(guild, channel)
        return _payload(kind, [thread_to_dict(t) for t in threads])

    target = snowflake(str(ns.target), field="target")
    reader = service.read_recent_messages if kind == "channel" else service.read_thread
    messages = await reader(
        guild, target, hours_back=int(ns.hours), max_messages=int(ns.limit)
    )
    return _payload(kind, [message_to_dict(m) for m in messages])


def _payload(kind: str, items: list[JsonDict]) -> JsonDict:
    return {"kind": kind, "count": len(items), "items": items}


# ---------------------------------------------------------------- wiring


def _build_session_store(config: Config) -> SessionStore:
    vault = OSKeyringVault(service=config.keyring_service, user=config.keyring_user)
    return EncryptedFileSessionStore(path=config.session_path, vault=vault)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
