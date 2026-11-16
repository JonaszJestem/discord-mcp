"""EventBuffer cursors, filtering, eviction and waiting."""

import asyncio
from datetime import datetime, timezone

import pytest

from discord_mcp import events as events_mod
from discord_mcp.errors import InvalidCursor
from discord_mcp.events import EventBuffer, event_filter, parse_kinds
from discord_mcp.models import EventKind, Message, MessageEvent, snowflake

ALL = event_filter(frozenset({"message"}))


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
        first = buf.read(None, match=ALL, limit=10)
        buf.publish(_event("2"))
        assert _ids(buf.read(first.cursor, match=ALL, limit=10)) == ["2"]

    def test_cursor_skips_non_matching_events(self):
        buf = EventBuffer(epoch="e")
        buf.publish(_event("1"))
        batch = buf.read(None, match=event_filter(frozenset({"mention"})), limit=10)
        assert batch.events == []
        assert batch.cursor == "e:1"

    def test_limit_stops_cursor_at_last_returned(self):
        buf = EventBuffer(epoch="e")
        for i in range(5):
            buf.publish(_event(str(i)))
        batch = buf.read(None, match=ALL, limit=2)
        assert _ids(batch) == ["0", "1"]
        assert _ids(buf.read(batch.cursor, match=ALL, limit=10)) == ["2", "3", "4"]

    def test_eviction_reports_missed(self):
        buf = EventBuffer(maxlen=2, epoch="e")
        cursor = buf.cursor
        for i in range(5):
            buf.publish(_event(str(i)))
        batch = buf.read(cursor, match=ALL, limit=10)
        assert _ids(batch) == ["3", "4"]
        assert batch.missed == 3

    def test_cursor_from_previous_process_resets(self):
        old = EventBuffer(epoch="old")
        old.publish(_event("1"))
        buf = EventBuffer(epoch="new")
        buf.publish(_event("2"))
        batch = buf.read(old.cursor, match=ALL, limit=10)
        assert batch.reset is True
        assert _ids(batch) == ["2"]

    @pytest.mark.parametrize("bad", ["", "nocolon", "e:x", "e:-1", "e:5"])
    def test_rejects_foreign_or_future_cursor(self, bad: str):
        with pytest.raises(InvalidCursor):
            EventBuffer(epoch="e").read(bad, match=ALL, limit=10)

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


class TestFilter:
    def test_kinds_and_channels_both_apply(self):
        match = event_filter(frozenset({"mention"}), [snowflake("42")])
        assert match(_event("1", "mention"))
        assert not match(_event("2"))
        assert not match(_event("3", "mention", channel="43"))

    def test_parse_kinds_defaults_and_rejects_unknown(self):
        assert parse_kinds(None) == {"mention", "dm"}
        with pytest.raises(ValueError, match="bogus"):
            parse_kinds(["dm", "bogus"])


class TestWait:
    async def test_returns_when_matching_event_is_published(self):
        buf = EventBuffer(epoch="e")
        task = asyncio.create_task(buf.wait(None, match=ALL, limit=10, timeout_s=5))
        await asyncio.sleep(0)
        buf.publish(_event("1"))
        batch = await asyncio.wait_for(task, 1)
        assert _ids(batch) == ["1"]
        assert batch.timed_out is False

    async def test_since_none_ignores_already_buffered(self):
        buf = EventBuffer(epoch="e")
        buf.publish(_event("old"))
        batch = await buf.wait(None, match=ALL, limit=10, timeout_s=0.05)
        assert batch.events == []
        assert batch.timed_out is True
        assert batch.cursor == "e:1"

    async def test_keeps_waiting_past_non_matching_events(self):
        buf = EventBuffer(epoch="e")
        mentions = event_filter(frozenset({"mention"}))
        task = asyncio.create_task(
            buf.wait(None, match=mentions, limit=10, timeout_s=5)
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
            None, match=ALL, limit=10, timeout_s=0.1, heartbeat=heartbeat
        )
        assert batch.timed_out
        assert len(beats) >= 3
        assert beats == sorted(beats)

    async def test_every_concurrent_waiter_receives_the_event(self):
        buf = EventBuffer(epoch="e")
        waiters = [
            asyncio.create_task(buf.wait(None, match=ALL, limit=10, timeout_s=5))
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
            buf.wait(None, match=ALL, limit=10, timeout_s=5, should_stop=lambda: stop)
        )
        await asyncio.sleep(0)
        stop = True
        buf.wake()
        batch = await asyncio.wait_for(task, 1)
        assert batch.events == [] and batch.timed_out is False

    async def test_missed_and_reset_describe_the_callers_cursor(self):
        buf = EventBuffer(maxlen=1, epoch="e")
        buf.publish(_event("1"))
        buf.publish(_event("2"))
        batch = await buf.wait("e:0", match=ALL, limit=10, timeout_s=1)
        assert _ids(batch) == ["2"]
        assert batch.missed == 1
