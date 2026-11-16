"""In-memory event buffer with resumable cursors. No I/O.

A cursor is `<epoch>:<seq>`. The epoch changes on every process start, so a
cursor kept by a client across a server restart is detected (`reset`) instead
of silently skipping or replaying events.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import cast, get_args

from .errors import InvalidCursor
from .models import EventKind, MessageEvent, Snowflake

DEFAULT_BUFFER_SIZE = 10_000
HEARTBEAT_S = 30.0

EventFilter = Callable[[MessageEvent], bool]
EVENT_KINDS: tuple[EventKind, ...] = get_args(EventKind)
DEFAULT_KINDS: frozenset[EventKind] = frozenset({"mention", "dm"})


def parse_kinds(raw: Collection[str] | None) -> frozenset[EventKind]:
    """Validate requested kinds; raises ValueError naming the unknown ones."""
    if not raw:
        return DEFAULT_KINDS
    unknown = sorted(set(raw) - set(EVENT_KINDS))
    if unknown:
        raise ValueError(f"unknown kinds {unknown}; supported: {list(EVENT_KINDS)}")
    return frozenset(cast(Collection[EventKind], raw))


def event_filter(
    kinds: frozenset[EventKind], channel_ids: Collection[Snowflake] | None = None
) -> EventFilter:
    channels = frozenset(channel_ids or ())

    def match(event: MessageEvent) -> bool:
        if channels and event.message.channel_id not in channels:
            return False
        return not kinds.isdisjoint(event.kinds)

    return match


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    seq: int
    event: MessageEvent


@dataclass(frozen=True, slots=True)
class EventBatch:
    events: list[SequencedEvent]
    cursor: str
    # Events evicted before the caller read them; they are gone for good.
    missed: int
    # The cursor came from a previous server process.
    reset: bool
    timed_out: bool = False


class EventBuffer:
    def __init__(
        self, *, maxlen: int = DEFAULT_BUFFER_SIZE, epoch: str | None = None
    ) -> None:
        self._events: deque[SequencedEvent] = deque(maxlen=maxlen)
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
        if len(self._events) == self._events.maxlen:
            self._seen_ids.discard(self._events[0].event.message.id)
        self._seq += 1
        self._events.append(SequencedEvent(self._seq, event))
        self._seen_ids.add(message_id)
        self.wake()

    def wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def read(self, since: str | None, *, match: EventFilter, limit: int) -> EventBatch:
        """Matching events after `since`; None reads everything still buffered."""
        since_seq, reset = self._parse(since)
        oldest = self._events[0].seq if self._events else self._seq + 1
        missed = max(0, oldest - since_seq - 1)
        picked: list[SequencedEvent] = []
        cursor_seq = self._seq
        for item in self._events:
            if item.seq <= since_seq or not match(item.event):
                continue
            picked.append(item)
            if len(picked) == limit:
                cursor_seq = item.seq
                break
        return EventBatch(
            events=picked,
            cursor=f"{self._epoch}:{cursor_seq}",
            missed=missed,
            reset=reset,
        )

    async def wait(
        self,
        since: str | None,
        *,
        match: EventFilter,
        limit: int,
        timeout_s: float,
        heartbeat: Callable[[float], Awaitable[None]] | None = None,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> EventBatch:
        """Block until a matching event arrives after `since`, or `timeout_s` passes.

        `since=None` means "from now": only events published after the call.
        """
        start_cursor = since if since is not None else self.cursor
        first = self.read(start_cursor, match=match, limit=limit)
        deadline = time.monotonic() + timeout_s
        started = time.monotonic()
        batch = first
        while not batch.events and not should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _with_flags(batch, first, timed_out=True)
            changed = self._changed
            try:
                await asyncio.wait_for(changed.wait(), min(remaining, HEARTBEAT_S))
            except TimeoutError:
                if heartbeat is not None and time.monotonic() < deadline:
                    await heartbeat(time.monotonic() - started)
            batch = self.read(batch.cursor, match=match, limit=limit)
        return _with_flags(batch, first, timed_out=False)

    def _parse(self, cursor: str | None) -> tuple[int, bool]:
        if cursor is None:
            return 0, False
        epoch, sep, raw_seq = cursor.partition(":")
        if not sep or not raw_seq.isdigit():
            raise InvalidCursor(f"not an event cursor: {cursor!r}")
        if epoch != self._epoch:
            return 0, True
        seq = int(raw_seq)
        if seq > self._seq:
            raise InvalidCursor(f"cursor {cursor!r} is ahead of this stream")
        return seq, False


def _with_flags(batch: EventBatch, first: EventBatch, *, timed_out: bool) -> EventBatch:
    # `missed`/`reset` describe the caller's cursor, which only the first read saw.
    return EventBatch(
        events=batch.events,
        cursor=batch.cursor,
        missed=first.missed,
        reset=first.reset,
        timed_out=timed_out,
    )
