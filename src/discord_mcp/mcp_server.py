"""FastMCP tool registration. Every tool delegates to DiscordService.

If you add a new Discord use case, add it to DiscordService and register a
thin tool here — this file should stay boring on purpose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config
from .errors import DiscordMcpError, InvalidSnowflake, SessionExpired
from .models import Message, snowflake
from .discord import DiscordService


MAX_MESSAGES_HARD_LIMIT = 1000
MAX_HOURS_BACK = 8760  # one year


def create_mcp_server(service: DiscordService, config: Config) -> FastMCP:
    """Build a FastMCP server whose tool handlers delegate to `service`."""

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[DiscordService]:
        try:
            yield service
        finally:
            await service.close()

    mcp = FastMCP("discord-mcp", lifespan=lifespan)

    @mcp.tool()
    async def get_servers() -> list[dict[str, str]] | dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """List all Discord servers (guilds) you have access to."""
        try:
            guilds = await service.list_guilds()
        except DiscordMcpError as e:
            return _to_error(e)
        return [{"id": g.id, "name": g.name} for g in guilds]

    @mcp.tool()
    async def get_channels(server_id: str) -> list[dict[str, str]] | dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """List all channels in a specific Discord server."""
        try:
            guild_id = snowflake(server_id, field="server_id")
            channels = await service.list_channels(guild_id)
        except DiscordMcpError as e:
            return _to_error(e)
        return [{"id": c.id, "name": c.name, "type": str(c.type)} for c in channels]

    @mcp.tool()
    async def read_messages(  # pyright: ignore[reportUnusedFunction]
        server_id: str,
        channel_id: str,
        max_messages: int,
        hours_back: int = 24,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Read recent messages from a specific channel."""
        if not 1 <= hours_back <= MAX_HOURS_BACK:
            return _validation_error(
                f"hours_back must be between 1 and {MAX_HOURS_BACK}"
            )
        if not 1 <= max_messages <= MAX_MESSAGES_HARD_LIMIT:
            return _validation_error(
                f"max_messages must be between 1 and {MAX_MESSAGES_HARD_LIMIT}"
            )
        try:
            guild_id = snowflake(server_id, field="server_id")
            channel = snowflake(channel_id, field="channel_id")
            messages = await service.read_recent_messages(
                guild_id, channel, hours_back=hours_back, max_messages=max_messages
            )
        except DiscordMcpError as e:
            return _to_error(e)
        return [_message_dict(m) for m in messages]

    @mcp.tool()
    async def get_pinned_messages(  # pyright: ignore[reportUnusedFunction]
        server_id: str, channel_id: str
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """List pinned messages in a specific Discord channel."""
        try:
            guild = snowflake(server_id, field="server_id")
            channel = snowflake(channel_id, field="channel_id")
            messages = await service.list_pinned(guild, channel)
        except DiscordMcpError as e:
            return _to_error(e)
        return [_message_dict(m) for m in messages]

    @mcp.tool()
    async def get_threads(  # pyright: ignore[reportUnusedFunction]
        server_id: str, channel_id: str
    ) -> list[dict[str, str]] | dict[str, Any]:
        """List active threads in a specific Discord channel."""
        try:
            guild = snowflake(server_id, field="server_id")
            channel = snowflake(channel_id, field="channel_id")
            threads = await service.list_threads(guild, channel)
        except DiscordMcpError as e:
            return _to_error(e)
        return [
            {"id": t.id, "name": t.name, "parent_channel_id": t.parent_channel_id}
            for t in threads
        ]

    @mcp.tool()
    async def read_thread(  # pyright: ignore[reportUnusedFunction]
        server_id: str,
        thread_id: str,
        max_messages: int,
        hours_back: int = 24,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Read recent messages inside a thread (threads reuse the channel ID space)."""
        if not 1 <= hours_back <= MAX_HOURS_BACK:
            return _validation_error(
                f"hours_back must be between 1 and {MAX_HOURS_BACK}"
            )
        if not 1 <= max_messages <= MAX_MESSAGES_HARD_LIMIT:
            return _validation_error(
                f"max_messages must be between 1 and {MAX_MESSAGES_HARD_LIMIT}"
            )
        try:
            guild = snowflake(server_id, field="server_id")
            thread = snowflake(thread_id, field="thread_id")
            messages = await service.read_thread(
                guild, thread, hours_back=hours_back, max_messages=max_messages
            )
        except DiscordMcpError as e:
            return _to_error(e)
        return [_message_dict(m) for m in messages]

    @mcp.tool()
    async def search_messages(  # pyright: ignore[reportUnusedFunction]
        server_id: str,
        query: str,
        limit: int = 25,
        channel_id: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Search messages in a server (optionally scoped to one channel)."""
        if not query.strip():
            return _validation_error("query must not be empty")
        if not 1 <= limit <= MAX_MESSAGES_HARD_LIMIT:
            return _validation_error(
                f"limit must be between 1 and {MAX_MESSAGES_HARD_LIMIT}"
            )
        try:
            guild = snowflake(server_id, field="server_id")
            scope = snowflake(channel_id, field="channel_id") if channel_id else None
            messages = await service.search_messages(
                guild, query, channel_id=scope, limit=limit
            )
        except DiscordMcpError as e:
            return _to_error(e)
        return [_message_dict(m) for m in messages]

    @mcp.tool()
    async def get_mentions(  # pyright: ignore[reportUnusedFunction]
        limit: int = 25,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """List recent @mentions of you across all servers."""
        if not 1 <= limit <= MAX_MESSAGES_HARD_LIMIT:
            return _validation_error(
                f"limit must be between 1 and {MAX_MESSAGES_HARD_LIMIT}"
            )
        try:
            messages = await service.list_mentions(limit=limit)
        except DiscordMcpError as e:
            return _to_error(e)
        return [_message_dict(m) for m in messages]

    if not config.read_only:

        @mcp.tool()
        async def reply_to_message(  # pyright: ignore[reportUnusedFunction]
            server_id: str, channel_id: str, message_id: str, content: str
        ) -> dict[str, Any]:
            """Reply to a specific message in a channel (creates a Discord reply ref)."""
            if not content:
                return _validation_error("Reply content cannot be empty")
            if not message_id:
                return _validation_error("message_id is required")
            try:
                guild = snowflake(server_id, field="server_id")
                channel = snowflake(channel_id, field="channel_id")
                reply_id = await service.reply_to_message(
                    guild, channel, message_id, content
                )
            except DiscordMcpError as e:
                return _to_error(e)
            return {"reply_id": reply_id, "status": "sent"}

        @mcp.tool()
        async def react_to_message(  # pyright: ignore[reportUnusedFunction]
            server_id: str, channel_id: str, message_id: str, emoji: str
        ) -> dict[str, Any]:
            """Add an emoji reaction to a message. `emoji` is the name, e.g. 'thumbsup'."""
            if not emoji:
                return _validation_error("emoji is required")
            if not message_id:
                return _validation_error("message_id is required")
            try:
                guild = snowflake(server_id, field="server_id")
                channel = snowflake(channel_id, field="channel_id")
                await service.react_to_message(guild, channel, message_id, emoji)
            except DiscordMcpError as e:
                return _to_error(e)
            return {"status": "reacted", "emoji": emoji}

        @mcp.tool()
        async def send_message(  # pyright: ignore[reportUnusedFunction]
            server_id: str, channel_id: str, content: str
        ) -> dict[str, Any]:
            """Send a message to a Discord channel. Long messages are split."""
            if not content:
                return _validation_error("Message content cannot be empty")
            try:
                guild_id = snowflake(server_id, field="server_id")
                channel = snowflake(channel_id, field="channel_id")
            except DiscordMcpError as e:
                return _to_error(e)

            chunks = _chunk_message(content, limit=2000)
            message_ids: list[str] = []
            for chunk in chunks:
                try:
                    msg_id = await service.send_message(guild_id, channel, chunk)
                except DiscordMcpError as e:
                    return _to_error(e)
                message_ids.append(msg_id)
            return {
                "message_ids": message_ids,
                "status": "sent",
                "chunks": len(chunks),
                "total_length": len(content),
            }

    return mcp


def _to_error(exc: DiscordMcpError) -> dict[str, Any]:
    if isinstance(exc, SessionExpired):
        return {
            "error": "discord_session_expired",
            "message": str(exc),
            "action": "Ask the user to run `discord-mcp login` in a terminal.",
        }
    if isinstance(exc, InvalidSnowflake):
        return {"error": "invalid_snowflake", "message": str(exc)}
    return {"error": type(exc).__name__, "message": str(exc)}


def _validation_error(message: str) -> dict[str, Any]:
    return {"error": "validation_error", "message": message}


def _message_dict(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "content": m.content,
        "author_name": m.author_name,
        "channel_id": m.channel_id,
        "timestamp": m.timestamp.isoformat(),
        "attachments": m.attachments,
    }


def _chunk_message(content: str, *, limit: int) -> list[str]:
    """Split `content` into ≤`limit`-char chunks, preferring newline boundaries."""
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    current = ""
    for line in content.split("\n"):
        piece = line if len(line) <= limit else _split_long_line(line, limit=limit)
        for part in piece if isinstance(piece, list) else [piece]:
            candidate = f"{current}\n{part}" if current else part
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = part
    if current:
        chunks.append(current)
    return chunks


def _split_long_line(line: str, *, limit: int) -> list[str]:
    """Break a single line longer than `limit` at word boundaries when possible."""
    parts: list[str] = []
    current = ""
    for word in line.split(" "):
        if len(word) > limit:
            if current:
                parts.append(current)
                current = ""
            for i in range(0, len(word), limit):
                parts.append(word[i : i + limit])
            continue
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = word
    if current:
        parts.append(current)
    return parts
