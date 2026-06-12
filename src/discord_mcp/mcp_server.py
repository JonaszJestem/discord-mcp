"""FastMCP tool registration. Every tool delegates to DiscordService.

If you add a new Discord use case, add it to DiscordService and register a
thin tool here — this file should stay boring on purpose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Config
from .errors import DiscordMcpError, InvalidSnowflake, SessionExpired
from .grouping import group_channels, group_conversations
from .models import Message, snowflake
from .discord import DiscordService


MAX_MESSAGES_HARD_LIMIT = 1000
MAX_HOURS_BACK = 8760  # one year
DEFAULT_LIMIT = 25  # used when the caller omits `limit` on an unbounded query
GROUP_BY_VALUES = ("conversation", "channel", "none")
# reply context is always attached; these are opt-in extras.
INCLUDE_VALUES = ("mentions", "context")


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
        query: str = "",
        limit: int | None = None,
        channel_id: str | None = None,
        author_id: str | None = None,
        hours_back: int | None = None,
        group_by: str = "conversation",
        include: list[str] | None = None,
        deep: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Search messages in a server, optionally filtered.

        Filters combine: `query` (text), `channel_id` (one channel),
        `author_id` (a user snowflake), and `hours_back` (only messages newer
        than N hours). `query` may be empty as long as `author_id` is set.

        Omit `limit` to get the whole `hours_back` window (up to a 1000-message
        hard cap); without `hours_back` an omitted `limit` defaults to 25.

        `group_by` shapes output: "conversation" (default, channel→time bursts),
        "channel", or "none" (flat). Each message carries who it replied to;
        pass include=["mentions"] to also list @-mentions. `deep` additionally
        walks recent threads (only with `author_id`) for posts search misses.
        """
        if not query.strip() and not author_id:
            return _validation_error("provide a query or an author_id")
        if limit is not None and not 1 <= limit <= MAX_MESSAGES_HARD_LIMIT:
            return _validation_error(
                f"limit must be between 1 and {MAX_MESSAGES_HARD_LIMIT}"
            )
        if hours_back is not None and not 1 <= hours_back <= MAX_HOURS_BACK:
            return _validation_error(
                f"hours_back must be between 1 and {MAX_HOURS_BACK}"
            )
        shape_error = _validate_shape(group_by, include)
        if shape_error is not None:
            return shape_error
        try:
            guild = snowflake(server_id, field="server_id")
            scope = snowflake(channel_id, field="channel_id") if channel_id else None
            author = snowflake(author_id, field="author_id") if author_id else None
            after = _cutoff(hours_back)
            messages = await service.search_messages(
                guild,
                query,
                channel_id=scope,
                limit=_effective_limit(limit, time_bounded=hours_back is not None),
                author_id=author,
                after=after,
                deep=deep,
                context="context" in (include or []),
            )
        except DiscordMcpError as e:
            return _to_error(e)
        return _render(messages, group_by=group_by, show_author=True, include=include)

    @mcp.tool()
    async def get_user_messages(  # pyright: ignore[reportUnusedFunction]
        server_id: str,
        author: str,
        hours_back: int = 24,
        limit: int | None = None,
        channel_id: str | None = None,
        group_by: str = "conversation",
        include: list[str] | None = None,
        deep: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Messages a person posted in a server within the last `hours_back`.

        `author` is a user snowflake (e.g. '125570309171445760') or a username
        we've already seen in this server (resolved from the local cache).

        Returns the whole `hours_back` window by default (up to a 1000-message
        hard cap); pass `limit` only to cap the count below that.

        `group_by` shapes output: "conversation" (default, channel→time bursts),
        "channel", or "none" (flat). Each message carries who it replied to;
        pass include=["mentions"] to also list @-mentions. `deep` additionally
        walks recent threads for posts the search index misses.
        """
        if not author.strip():
            return _validation_error("author is required")
        if limit is not None and not 1 <= limit <= MAX_MESSAGES_HARD_LIMIT:
            return _validation_error(
                f"limit must be between 1 and {MAX_MESSAGES_HARD_LIMIT}"
            )
        if not 1 <= hours_back <= MAX_HOURS_BACK:
            return _validation_error(
                f"hours_back must be between 1 and {MAX_HOURS_BACK}"
            )
        shape_error = _validate_shape(group_by, include)
        if shape_error is not None:
            return shape_error
        resolved = service.resolve_person(author)
        if resolved is None:
            return {
                "error": "user_not_resolved",
                "message": (
                    f"'{author}' is not a known user. Pass their numeric Discord "
                    "ID, or read a channel they've posted in so the cache learns them."
                ),
            }
        try:
            guild = snowflake(server_id, field="server_id")
            author_sf = snowflake(resolved, field="author_id")
            scope = snowflake(channel_id, field="channel_id") if channel_id else None
            messages = await service.search_messages(
                guild,
                "",
                channel_id=scope,
                limit=_effective_limit(limit, time_bounded=True),
                author_id=author_sf,
                after=_cutoff(hours_back),
                deep=deep,
                context="context" in (include or []),
            )
        except DiscordMcpError as e:
            return _to_error(e)
        # Context pulls in other people's messages, so label authors.
        show_author = "context" in (include or [])
        return _render(
            messages, group_by=group_by, show_author=show_author, include=include
        )

    @mcp.tool()
    async def resolve_user(  # pyright: ignore[reportUnusedFunction]
        name: str,
    ) -> dict[str, Any]:
        """Resolve a username to a Discord ID from the local discovery cache."""
        resolved = service.resolve_person(name)
        if resolved is None:
            return {"error": "user_not_resolved", "name": name}
        return {"name": name, "id": resolved}

    @mcp.tool()
    async def get_known_people(  # pyright: ignore[reportUnusedFunction]
    ) -> list[dict[str, str]]:
        """List users discovered so far (id, name, last seen) from the cache."""
        return service.known_people()

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


def _effective_limit(limit: int | None, *, time_bounded: bool) -> int:
    """Resolve how many messages to walk for.

    When the caller passes `limit`, honour it. When they omit it, a time-bounded
    query (a `hours_back`/`after` floor is set) walks the *whole window* up to the
    hard cap, so the time range — not an arbitrary count — is the boundary. An
    unbounded query keeps a small default so it can't accidentally pull 1000 rows.
    """
    if limit is not None:
        return limit
    return MAX_MESSAGES_HARD_LIMIT if time_bounded else DEFAULT_LIMIT


def _cutoff(hours_back: int | None) -> datetime | None:
    if hours_back is None:
        return None
    return datetime.now(timezone.utc) - timedelta(hours=hours_back)


def _validate_shape(
    group_by: str, include: list[str] | None
) -> dict[str, Any] | None:
    """Reject unknown `group_by` / `include` values; None means valid."""
    if group_by not in GROUP_BY_VALUES:
        return _validation_error(
            f"group_by must be one of {list(GROUP_BY_VALUES)}"
        )
    unknown = [i for i in (include or []) if i not in INCLUDE_VALUES]
    if unknown:
        return _validation_error(
            f"unknown include {unknown}; supported: {list(INCLUDE_VALUES)}"
        )
    return None


def _render(
    messages: list[Message],
    *,
    group_by: str,
    show_author: bool,
    include: list[str] | None,
) -> list[dict[str, Any]]:
    """Shape messages per `group_by`; reply context is always attached."""
    include_mentions = "mentions" in (include or [])
    if group_by == "none":
        return [_message_dict(m, include_mentions=include_mentions) for m in messages]
    if group_by == "channel":
        return group_channels(
            messages, show_author=show_author, include_mentions=include_mentions
        )
    return group_conversations(
        messages, show_author=show_author, include_mentions=include_mentions
    )


def _message_dict(m: Message, *, include_mentions: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": m.id,
        "content": m.content,
        "author_name": m.author_name,
        "author_id": m.author_id,
        "channel_id": m.channel_id,
        "timestamp": m.timestamp.isoformat(),
        "attachments": m.attachments,
    }
    if m.reply_to is not None:
        out["reply_to"] = {
            "message_id": m.reply_to.message_id,
            "author_name": m.reply_to.author_name,
            "author_id": m.reply_to.author_id,
            "content": m.reply_to.content,
        }
    if include_mentions and m.mention_names:
        out["mentions"] = list(m.mention_names)
    return out


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
