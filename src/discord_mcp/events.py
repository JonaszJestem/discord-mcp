"""In-memory event buffer with resumable cursors. No I/O.

A cursor is `<epoch>:<seq>`. The epoch changes on every process start, so a
cursor kept by a client across a server restart is detected (`reset`) instead
of silently skipping or replaying events.
"""

from __future__ import annotations

import asyncio
import bisect
import heapq
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Iterator
from dataclasses import dataclass
from itertools import islice
from typing import cast, get_args

from .errors import InvalidCursor
from .models import EventKind, MessageEvent, Snowflake

DEFAULT_BUFFER_SIZE = 10_000
HEARTBEAT_S = 30.0
_MIN_WAIT_S = 0.01

EVENT_KINDS: tuple[EventKind, ...] = get_args(EventKind)
DEFAULT_KINDS: frozenset[EventKind] = frozenset({"mention", "dm"})
# Kept in their own ring so busy channels cannot evict them.
_PRIORITY_KINDS: frozenset[EventKind] = frozenset({"mention", "dm"})
_CURSOR_RE = re.compile(r"([^:]+):([0-9]+)")


def parse_kinds(raw: Collection[str] | None) -> frozenset[EventKind]:
    """Validate requested kinds; raises ValueError naming the unknown ones."""
    if not raw:
        return DEFAULT_KINDS
    unknown = sorted(set(raw) - set(EVENT_KINDS))
    if unknown:
        raise ValueError(f"unknown kinds {unknown}; supported: {list(EVENT_KINDS)}")
    return frozenset(cast(Collection[EventKind], raw))


@dataclass(frozen=True, slots=True)
class EventQuery:
    kinds: frozenset[EventKind] = DEFAULT_KINDS
    channel_ids: frozenset[Snowflake] = frozenset()

    def matches(self, event: MessageEvent) -> bool:
        if self.channel_ids and event.message.channel_id not in self.channel_ids:
            return False
        return not self.kinds.isdisjoint(event.kinds)


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    seq: int
    event: MessageEvent


@dataclass(frozen=True, slots=True)
class EventBatch:
    events: list[SequencedEvent]
    cursor: str
    gap: bool
    # The cursor came from a previous server process.
    reset: bool
    timed_out: bool = False


class _Ring:
    def __init__(self, maxlen: int) -> None:
        self.items: deque[SequencedEvent] = deque(maxlen=maxlen)
        self.evicted_upto = 0

    def append(self, item: SequencedEvent) -> SequencedEvent | None:
        evicted = self.items[0] if len(self.items) == self.items.maxlen else None
        if evicted is not None:
            self.evicted_upto = evicted.seq
        self.items.append(item)
        return evicted


class EventBuffer:
    def __init__(
        self, *, maxlen: int = DEFAULT_BUFFER_SIZE, epoch: str | None = None
    ) -> None:
        self._priority = _Ring(maxlen)
        self._rest = _Ring(maxlen)
        self._seen_ids: set[str] = set()
        self._epoch = epoch or secrets.token_hex(4)
        self._seq = 0
        self._changed = asyncio.Event()

    @property
    def cursor(self) -> str:
        return f"{self._epoch}:{self._seq}"

    def publish(self, event: MessageEvent) -> None:
        # A gateway RESUME can replay a message the buffer already holds.
        message_id = event.message.id
        if message_id in self._seen_ids:
            return
        self._seq += 1
        ring = self._priority if event.kinds & _PRIORITY_KINDS else self._rest
        evicted = ring.append(SequencedEvent(self._seq, event))
        if evicted is not None:
            self._seen_ids.discard(evicted.event.message.id)
        self._seen_ids.add(message_id)
        self.wake()

    def wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def read(self, since: str | None, *, query: EventQuery, limit: int) -> EventBatch:
        """Matching events after `since`; None reads everything still buffered."""
        since_seq, reset = self._parse(since)
        rings = self._rings_for(query)
        gap = since is not None and any(r.evicted_upto > since_seq for r in rings)
        picked: list[SequencedEvent] = []
        cursor_seq = self._seq
        unread = (_after(r.items, since_seq) for r in rings)
        for item in heapq.merge(*unread, key=lambda i: i.seq):
            if not query.matches(item.event):
                continue
            picked.append(item)
            if len(picked) == limit:
                cursor_seq = item.seq
                break
        return EventBatch(
            events=picked, cursor=f"{self._epoch}:{cursor_seq}", gap=gap, reset=reset
        )

    def _rings_for(self, query: EventQuery) -> list[_Ring]:
        rings = [self._priority]
        if "message" in query.kinds:
            rings.append(self._rest)
        return rings

    async def wait(
        self,
        since: str | None,
        *,
        query: EventQuery,
        limit: int,
        timeout_s: float,
        heartbeat: Callable[[float], Awaitable[None]] | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> EventBatch:
        """Block until a matching event arrives after `since`, or `timeout_s` passes.

        `since=None` means "from now": only events published after the call.
        """
        start_cursor = since if since is not None else self.cursor
        first = self.read(start_cursor, query=query, limit=limit)
        started = time.monotonic()
        deadline = started + timeout_s
        # Clock-based: non-matching traffic must not starve the heartbeat.
        next_beat = started + HEARTBEAT_S
        batch = first
        while not batch.events and not should_stop():
            now = time.monotonic()
            if now >= deadline:
                return _with_flags(batch, first, timed_out=True)
            if heartbeat is not None and now >= next_beat:
                await heartbeat(now - started)
                next_beat = now + HEARTBEAT_S
            wake_at = deadline if heartbeat is None else min(deadline, next_beat)
            # A slow heartbeat can leave wake_at in the past; never spin on it.
            wait_s = max(_MIN_WAIT_S, wake_at - time.monotonic())
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), wait_s)
            except TimeoutError:
                pass
            batch = self.read(batch.cursor, query=query, limit=limit)
        return _with_flags(batch, first, timed_out=False)

    def _parse(self, cursor: str | None) -> tuple[int, bool]:
        if cursor is None:
            return 0, False
        parsed = _CURSOR_RE.fullmatch(cursor)
        if parsed is None:
            raise InvalidCursor(f"not an event cursor: {cursor!r}")
        if parsed[1] != self._epoch:
            return 0, True
        seq = int(parsed[2])
        if seq > self._seq:
            raise InvalidCursor(f"cursor {cursor!r} is ahead of this stream")
        return seq, False


def _after(items: deque[SequencedEvent], seq: int) -> Iterator[SequencedEvent]:
    start = bisect.bisect_right(items, seq, key=lambda i: i.seq)
    return islice(items, start, None)


def _with_flags(batch: EventBatch, first: EventBatch, *, timed_out: bool) -> EventBatch:
    # `gap`/`reset` describe the caller's cursor, which only the first read saw.
    return EventBatch(
        events=batch.events,
        cursor=batch.cursor,
        gap=first.gap,
        reset=first.reset,
        timed_out=timed_out,
    )
