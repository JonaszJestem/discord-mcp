"""DiscordService: the use-case boundary for MCP tools.

Takes validated Snowflakes and produces domain types. Owns the browser pool,
acquires a driver per request, and evicts broken drivers so callers
(mcp_server) stay thin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from ..cache import DiscoveryCache
from ..grouping import split_bursts
from ..models import Channel, Guild, Message, Snowflake, Thread
from .browser_driver import DiscordBrowserDriver
from .browser_pool import BrowserPool

# Deep fan-out reads threads one navigation each, so cap how many we walk.
MAX_DEEP_THREADS = 20
# Surrounding-context fetch bounds (include=["context"]).
CONTEXT_PAD_MINUTES = 5  # widen each burst window by this much either side
CONTEXT_MAX_BURSTS = 15  # enrich at most this many (largest) bursts
CONTEXT_WINDOW_LIMIT = 60  # messages fetched per burst window
CONTEXT_BUDGET = 500  # max surrounding messages added overall


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
        deep: bool = False,
        context: bool = False,
    ) -> list[Message]:
        """Guild search, optionally augmented by deep fan-out and/or context.

        `deep` reads recent threads via the messages API and merges in the
        target author's posts that the search index misses. `context` adds the
        surrounding conversation (what *others* said around each burst). Both
        only apply when `author_id` is set.
        """
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
        if deep and author_id is not None:
            thread_messages = await self._with_driver(
                lambda d: self._deep_thread_messages(
                    d, guild_id, author_id, after, limit
                )
            )
            messages = _merge_dedupe(messages, thread_messages, limit=limit)
        if context and author_id is not None:
            surrounding = await self._with_driver(
                lambda d: self._context_messages(d, messages)
            )
            messages = _merge_with_context(
                messages, surrounding, budget=CONTEXT_BUDGET
            )
        self._remember(messages)
        return messages

    async def _context_messages(
        self, driver: DiscordBrowserDriver, author_messages: list[Message]
    ) -> list[Message]:
        """Surrounding channel messages around the author's busiest bursts."""
        windows: list[tuple[Snowflake, datetime, datetime, int]] = []
        by_channel: dict[Snowflake, list[Message]] = {}
        for m in author_messages:
            by_channel.setdefault(m.channel_id, []).append(m)
        pad = timedelta(minutes=CONTEXT_PAD_MINUTES)
        for channel_id, msgs in by_channel.items():
            for burst in split_bursts(msgs):
                windows.append(
                    (channel_id, burst[0].timestamp, burst[-1].timestamp, len(burst))
                )
        windows.sort(key=lambda w: w[3], reverse=True)

        out: list[Message] = []
        for channel_id, start, end, _ in windows[:CONTEXT_MAX_BURSTS]:
            out.extend(
                await driver.read_channel_messages_api(
                    channel_id,
                    after=start - pad,
                    before=end + pad,
                    limit=CONTEXT_WINDOW_LIMIT,
                )
            )
        return out

    async def _deep_thread_messages(
        self,
        driver: DiscordBrowserDriver,
        guild_id: Snowflake,
        author_id: Snowflake,
        after: datetime | None,
        limit: int,
    ) -> list[Message]:
        """Author's posts inside recently-active threads (search misses these)."""
        threads = await driver.list_all_threads(guild_id)
        active = [
            t
            for t in threads
            if after is None or t.last_activity is None or t.last_activity > after
        ]
        active.sort(
            key=lambda t: t.last_activity or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        out: list[Message] = []
        for t in active[:MAX_DEEP_THREADS]:
            msgs = await driver.read_channel_messages_api(
                t.id, after=after, limit=limit
            )
            out.extend(m for m in msgs if m.author_id == author_id)
        return out

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


def _merge_dedupe(
    primary: list[Message], extra: list[Message], *, limit: int
) -> list[Message]:
    """Merge two message lists, dedupe by id, newest-first, capped at `limit`."""
    seen: set[str] = set()
    out: list[Message] = []
    for m in sorted([*primary, *extra], key=lambda m: m.timestamp, reverse=True):
        if m.id in seen:
            continue
        seen.add(m.id)
        out.append(m)
        if len(out) >= limit:
            break
    return out


def _merge_with_context(
    focus: list[Message], context: list[Message], *, budget: int
) -> list[Message]:
    """Keep every focus message; add up to `budget` surrounding ones.

    Focus (the target author's) messages are never dropped — context only
    fills in around them — so the result is newest-first and deduped.
    """
    seen = {m.id for m in focus}
    out = list(focus)
    added = 0
    for m in sorted(context, key=lambda m: m.timestamp, reverse=True):
        if m.id in seen:
            continue
        seen.add(m.id)
        out.append(m)
        added += 1
        if added >= budget:
            break
    out.sort(key=lambda m: m.timestamp, reverse=True)
    return out
