"""EventBuffer cursors, filtering, eviction and waiting."""

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from discord_mcp import events as events_mod
from discord_mcp.errors import InvalidCursor
from discord_mcp.events import EventBuffer, EventQuery, parse_kinds
from discord_mcp.models import EventKind, Message, MessageEvent, snowflake

ALL = EventQuery(frozenset({"message"}))


def _event(mid: str, *kinds: EventKind, channel: str = "42") -> MessageEvent:
    return MessageEvent(
        kinds=frozenset({"message", *kinds}),
        guild_id=None,
        message=Message(
            id=mid,
            content="x",
            author_name="a",
            channel_id=snowflake(channel),
            timestamp=datetime(2026, 11, 16, tzinfo=timezone.utc),
            attachments=[],
        ),
    )


def _ids(batch: events_mod.EventBatch) -> list[str]:
    return [e.event.message.id for e in batch.events]


class TestRead:
    def test_cursor_resumes_after_last_seen(self):
        buf = EventBuffer(epoch="e")
        buf.publish(_event("1"))
        first = buf.read(None, query=ALL, limit=10)
        buf.publish(_event("2"))
        assert _ids(buf.read(first.cursor, query=ALL, limit=10)) == ["2"]

    def test_cursor_skips_non_matching_events(self):
        buf = EventBuffer(epoch="e")
        buf.publish(_event("1"))
        batch = buf.read(None, query=EventQuery(frozenset({"mention"})), limit=10)
        assert batch.events == []
        assert batch.cursor == "e:1"

    def test_limit_stops_cursor_at_last_returned(self):
        buf = EventBuffer(epoch="e")
        for i in range(5):
            buf.publish(_event(str(i)))
        batch = buf.read(None, query=ALL, limit=2)
        assert _ids(batch) == ["0", "1"]
        assert _ids(buf.read(batch.cursor, query=ALL, limit=10)) == ["2", "3", "4"]

    def test_eviction_past_cursor_reports_gap(self):
        buf = EventBuffer(maxlen=2, epoch="e")
        cursor = buf.cursor
        for i in range(5):
            buf.publish(_event(str(i)))
        batch = buf.read(cursor, query=ALL, limit=10)
        assert _ids(batch) == ["3", "4"]
        assert batch.gap is True
        assert buf.read("e:3", query=ALL, limit=10).gap is False

    def test_priority_ring_eviction_reports_gap(self):
        buf = EventBuffer(maxlen=1, epoch="e")
        buf.publish(_event("m1", "mention"))
        buf.publish(_event("m2", "mention"))
        for query in (EventQuery(), ALL):
            batch = buf.read("e:0", query=query, limit=10)
            assert _ids(batch) == ["m2"] and batch.gap is True

    def test_reading_from_start_is_never_a_gap(self):
        buf = EventBuffer(maxlen=1, epoch="e")
        buf.publish(_event("1"))
        buf.publish(_event("2"))
        assert buf.read(None, query=ALL, limit=10).gap is False

    def test_busy_channels_cannot_evict_mentions(self):
        buf = EventBuffer(maxlen=3, epoch="e")
        cursor = buf.cursor
        buf.publish(_event("ping", "mention"))
        for i in range(10):
            buf.publish(_event(f"noise{i}"))
        mentions = buf.read(cursor, query=EventQuery(), limit=10)
        assert _ids(mentions) == ["ping"]
        assert mentions.gap is False
        everything = buf.read(cursor, query=ALL, limit=10)
        assert _ids(everything) == ["ping", "noise7", "noise8", "noise9"]
        assert everything.gap is True

    def test_cursor_from_previous_process_resets(self):
        old = EventBuffer(epoch="old")
        old.publish(_event("1"))
        buf = EventBuffer(epoch="new")
        buf.publish(_event("2"))
        batch = buf.read(old.cursor, query=ALL, limit=10)
        assert batch.reset is True
        assert _ids(batch) == ["2"]

    @pytest.mark.parametrize("bad", ["", "nocolon", "e:x", "e:-1", "e:5", "e:²"])
    def test_rejects_foreign_or_future_cursor(self, bad: str):
        with pytest.raises(InvalidCursor):
            EventBuffer(epoch="e").read(bad, query=ALL, limit=10)

    def test_resume_replay_is_deduplicated(self):
        buf = EventBuffer(epoch="e")
        buf.publish(_event("1"))
        buf.publish(_event("1"))
        assert buf.cursor == "e:1"

    def test_evicted_id_can_be_published_again(self):
        buf = EventBuffer(maxlen=1, epoch="e")
        buf.publish(_event("1"))
        buf.publish(_event("2"))
        buf.publish(_event("1"))
        assert buf.cursor == "e:3"


class TestQuery:
    def test_kinds_and_channels_both_apply(self):
        query = EventQuery(frozenset({"mention"}), frozenset({snowflake("42")}))
        assert query.matches(_event("1", "mention"))
        assert not query.matches(_event("2"))
        assert not query.matches(_event("3", "mention", channel="43"))

    def test_parse_kinds_defaults_and_rejects_unknown(self):
        assert parse_kinds(None) == {"mention", "dm"}
        with pytest.raises(ValueError, match="bogus"):
            parse_kinds(["dm", "bogus"])


class TestWait:
    async def test_returns_when_matching_event_is_published(self):
        buf = EventBuffer(epoch="e")
        task = asyncio.create_task(buf.wait(None, query=ALL, limit=10, timeout_s=5))
        await asyncio.sleep(0)
        buf.publish(_event("1"))
        batch = await asyncio.wait_for(task, 1)
        assert _ids(batch) == ["1"]
        assert batch.timed_out is False

    async def test_since_none_ignores_already_buffered(self):
        buf = EventBuffer(epoch="e")
        buf.publish(_event("old"))
        batch = await buf.wait(None, query=ALL, limit=10, timeout_s=0.05)
        assert batch.events == []
        assert batch.timed_out is True
        assert batch.cursor == "e:1"

    async def test_keeps_waiting_past_non_matching_events(self):
        buf = EventBuffer(epoch="e")
        mentions = EventQuery(frozenset({"mention"}))
        task = asyncio.create_task(
            buf.wait(None, query=mentions, limit=10, timeout_s=5)
        )
        await asyncio.sleep(0)
        buf.publish(_event("noise"))
        await asyncio.sleep(0.01)
        assert not task.done()
        buf.publish(_event("ping", "mention"))
        assert _ids(await asyncio.wait_for(task, 1)) == ["ping"]

    async def test_heartbeats_while_idle(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(events_mod, "HEARTBEAT_S", 0.01)
        beats: list[float] = []

        async def heartbeat(elapsed: float) -> None:
            beats.append(elapsed)

        batch = await EventBuffer(epoch="e").wait(
            None, query=ALL, limit=10, timeout_s=0.1, heartbeat=heartbeat
        )
        assert batch.timed_out
        assert len(beats) >= 3
        assert beats == sorted(beats)

    async def test_idle_wait_without_heartbeat_does_not_spin(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(events_mod, "HEARTBEAT_S", 0.01)
        buf = EventBuffer(epoch="e")
        reads = 0
        real_read = buf.read

        def counting_read(*args: Any, **kwargs: Any) -> events_mod.EventBatch:
            nonlocal reads
            reads += 1
            return real_read(*args, **kwargs)

        monkeypatch.setattr(buf, "read", counting_read)
        batch = await buf.wait(None, query=ALL, limit=10, timeout_s=0.2)
        assert batch.timed_out
        assert reads <= 3

    async def test_every_concurrent_waiter_receives_the_event(self):
        buf = EventBuffer(epoch="e")
        waiters = [
            asyncio.create_task(buf.wait(None, query=ALL, limit=10, timeout_s=5))
            for _ in range(25)
        ]
        await asyncio.sleep(0)
        buf.publish(_event("1"))
        results = await asyncio.wait_for(asyncio.gather(*waiters), 1)
        assert [_ids(r) for r in results] == [["1"]] * 25

    async def test_should_stop_ends_wait_on_wake(self):
        buf = EventBuffer(epoch="e")
        stop = False
        task = asyncio.create_task(
            buf.wait(None, query=ALL, limit=10, timeout_s=5, should_stop=lambda: stop)
        )
        await asyncio.sleep(0)
        stop = True
        buf.wake()
        batch = await asyncio.wait_for(task, 1)
        assert batch.events == [] and batch.timed_out is False

    async def test_gap_describes_the_callers_cursor(self):
        buf = EventBuffer(maxlen=1, epoch="e")
        buf.publish(_event("1"))
        buf.publish(_event("2"))
        batch = await buf.wait("e:0", query=ALL, limit=10, timeout_s=1)
        assert _ids(batch) == ["2"]
        assert batch.gap is True

    async def test_heartbeats_keep_coming_under_non_matching_traffic(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(events_mod, "HEARTBEAT_S", 0.03)
        buf = EventBuffer(epoch="e")
        beats: list[float] = []

        async def heartbeat(elapsed: float) -> None:
            beats.append(elapsed)

        async def noise() -> None:
            for i in range(30):
                buf.publish(_event(f"n{i}"))
                await asyncio.sleep(0.005)

        noisy = asyncio.create_task(noise())
        await buf.wait(
            None,
            query=EventQuery(),
            limit=10,
            timeout_s=0.15,
            heartbeat=heartbeat,
        )
        await noisy
        assert len(beats) >= 2

    async def test_cancelled_wait_propagates_and_leaves_buffer_usable(self):
        buf = EventBuffer(epoch="e")
        task = asyncio.create_task(buf.wait(None, query=ALL, limit=10, timeout_s=5))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        buf.publish(_event("1"))
        assert _ids(buf.read(None, query=ALL, limit=10)) == ["1"]
