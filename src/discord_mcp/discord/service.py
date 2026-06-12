"""DiscordService: the use-case boundary for MCP tools.

Takes validated Snowflakes and produces domain types. Owns the browser pool,
acquires a driver per request, and evicts broken drivers so callers
(mcp_server) stay thin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from ..cache import DiscoveryCache
from ..models import Channel, Guild, Message, Snowflake, Thread
from .browser_driver import DiscordBrowserDriver
from .browser_pool import BrowserPool


class DiscordService:
    """High-level Discord operations backed by a pool of browser drivers."""

    def __init__(
        self,
        pool: BrowserPool[DiscordBrowserDriver],
        cache: DiscoveryCache | None = None,
    ) -> None:
        self._pool = pool
        self._cache = cache

    async def close(self) -> None:
        await self._pool.close_all()

    async def list_guilds(self) -> list[Guild]:
        guilds = await self._with_driver(lambda d: d.list_guilds())
        if self._cache:
            self._cache.record_guilds(guilds)
        return guilds

    async def list_channels(self, guild_id: Snowflake) -> list[Channel]:
        channels = await self._with_driver(lambda d: d.list_channels(guild_id))
        if self._cache:
            self._cache.record_channels(channels)
        return channels

    async def read_recent_messages(
        self,
        guild_id: Snowflake,
        channel_id: Snowflake,
        *,
        hours_back: int,
        max_messages: int,
    ) -> list[Message]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        all_messages = await self._with_driver(
            lambda d: d.read_messages(guild_id, channel_id, limit=max_messages)
        )
        recent = [m for m in all_messages if m.timestamp > cutoff]
        self._remember(recent)
        return recent

    async def send_message(
        self, guild_id: Snowflake, channel_id: Snowflake, content: str
    ) -> str:
        return await self._with_driver(
            lambda d: d.send_message(guild_id, channel_id, content)
        )

    async def list_pinned(
        self, guild_id: Snowflake, channel_id: Snowflake
    ) -> list[Message]:
        return await self._with_driver(lambda d: d.list_pinned(guild_id, channel_id))

    async def list_threads(
        self, guild_id: Snowflake, channel_id: Snowflake
    ) -> list[Thread]:
        return await self._with_driver(lambda d: d.list_threads(guild_id, channel_id))

    async def read_thread(
        self,
        guild_id: Snowflake,
        thread_id: Snowflake,
        *,
        hours_back: int,
        max_messages: int,
    ) -> list[Message]:
        """Read messages inside a thread. Threads share the channel ID space,
        so we delegate to the regular read path with the thread ID as channel_id.
        """
        return await self.read_recent_messages(
            guild_id,
            thread_id,
            hours_back=hours_back,
            max_messages=max_messages,
        )

    async def search_messages(
        self,
        guild_id: Snowflake,
        query: str,
        *,
        channel_id: Snowflake | None,
        limit: int,
        author_id: Snowflake | None = None,
        after: datetime | None = None,
    ) -> list[Message]:
        messages = await self._with_driver(
            lambda d: d.search_messages(
                guild_id,
                query,
                channel_id=channel_id,
                limit=limit,
                author_id=author_id,
                after=after,
            )
        )
        self._remember(messages)
        return messages

    async def list_mentions(self, *, limit: int) -> list[Message]:
        messages = await self._with_driver(lambda d: d.list_mentions(limit=limit))
        self._remember(messages)
        return messages

    def resolve_person(self, name: str) -> str | None:
        """Map a username to a cached snowflake (numeric input passes through)."""
        if name.isdigit():
            return name
        return self._cache.resolve_person(name) if self._cache else None

    def known_people(self) -> list[dict[str, str]]:
        return self._cache.known_people() if self._cache else []

    def _remember(self, messages: list[Message]) -> None:
        if self._cache:
            self._cache.record_people(messages)

    async def reply_to_message(
        self,
        guild_id: Snowflake,
        channel_id: Snowflake,
        message_id: str,
        content: str,
    ) -> str:
        return await self._with_driver(
            lambda d: d.reply_to_message(guild_id, channel_id, message_id, content)
        )

    async def react_to_message(
        self,
        guild_id: Snowflake,
        channel_id: Snowflake,
        message_id: str,
        emoji: str,
    ) -> None:
        await self._with_driver(
            lambda d: d.react_to_message(guild_id, channel_id, message_id, emoji)
        )

    async def _with_driver[T](
        self, op: Callable[[DiscordBrowserDriver], Awaitable[T]]
    ) -> T:
        driver = await self._pool.acquire()
        broken = False
        try:
            return await op(driver)
        except BaseException:
            # Any failure (including SessionExpired) means this driver's
            # browser may be in a bad state. Evict + close rather than
            # re-queue; a fresh driver will be lazily created on next acquire.
            broken = True
            raise
        finally:
            await self._pool.release(driver, broken=broken)
