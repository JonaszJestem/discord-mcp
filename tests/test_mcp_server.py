"""Tests for the non-Playwright logic in mcp_server: chunking + error formatting."""

from datetime import datetime, timedelta, timezone

from discord_mcp.errors import InvalidSnowflake, SessionExpired
from discord_mcp.mcp_server import (
    DEFAULT_LIMIT,
    MAX_MESSAGES_HARD_LIMIT,
    _chunk_message,
    _cutoff,
    _effective_limit,
    _render,
    _to_error,
    _validate_shape,
)
from discord_mcp.models import Message, snowflake


class TestChunkMessage:
    def test_short_message_stays_one_chunk(self):
        assert _chunk_message("hello", limit=2000) == ["hello"]

    def test_exact_limit_stays_one_chunk(self):
        s = "a" * 2000
        assert _chunk_message(s, limit=2000) == [s]

    def test_splits_on_newlines_when_possible(self):
        content = "\n".join(["a" * 1500, "b" * 1500])
        chunks = _chunk_message(content, limit=2000)
        assert len(chunks) == 2
        assert all(len(c) <= 2000 for c in chunks)

    def test_splits_long_line_on_word_boundary(self):
        content = " ".join(["word"] * 1000)  # ~5000 chars
        chunks = _chunk_message(content, limit=2000)
        assert all(len(c) <= 2000 for c in chunks)
        assert sum(len(c) for c in chunks) >= len(content) - 10

    def test_handles_single_very_long_word(self):
        content = "a" * 5000
        chunks = _chunk_message(content, limit=2000)
        assert all(len(c) <= 2000 for c in chunks)


class TestToError:
    def test_session_expired_gets_actionable_message(self):
        err = _to_error(SessionExpired("gone"))
        assert err["error"] == "discord_session_expired"
        assert "discord-mcp login" in err["action"]

    def test_invalid_snowflake_distinct_code(self):
        err = _to_error(InvalidSnowflake("bad"))
        assert err["error"] == "invalid_snowflake"

    def test_generic_error_uses_class_name(self):
        from discord_mcp.errors import DiscordMcpError

        err = _to_error(DiscordMcpError("boom"))
        assert err["error"] == "DiscordMcpError"


class TestEffectiveLimit:
    def test_explicit_limit_is_honoured(self):
        assert _effective_limit(50, time_bounded=True) == 50
        assert _effective_limit(50, time_bounded=False) == 50

    def test_omitted_limit_walks_whole_window_when_time_bounded(self):
        assert _effective_limit(None, time_bounded=True) == MAX_MESSAGES_HARD_LIMIT

    def test_omitted_limit_defaults_small_when_unbounded(self):
        assert _effective_limit(None, time_bounded=False) == DEFAULT_LIMIT


class TestCutoff:
    def test_none_means_no_lower_bound(self):
        assert _cutoff(None) is None

    def test_returns_past_utc_instant(self):
        before = datetime.now(timezone.utc)
        cutoff = _cutoff(24)
        assert cutoff is not None
        assert cutoff.tzinfo is not None
        assert cutoff < before


def _m(mid: str, *, minutes: int, channel: str = "100") -> Message:
    return Message(
        id=mid,
        content="hi",
        author_name="Bob",
        channel_id=snowflake(channel),
        timestamp=datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
        + timedelta(minutes=minutes),
        attachments=[],
        author_id="1",
    )


class TestValidateShape:
    def test_accepts_known_values(self):
        assert _validate_shape("conversation", ["mentions"]) is None
        assert _validate_shape("none", None) is None

    def test_rejects_unknown_group_by(self):
        err = _validate_shape("topic", None)
        assert err is not None and err["error"] == "validation_error"

    def test_accepts_context_include(self):
        assert _validate_shape("conversation", ["context", "mentions"]) is None

    def test_rejects_unknown_include(self):
        err = _validate_shape("conversation", ["bogus"])
        assert err is not None and "bogus" in err["message"]


class TestRender:
    def test_none_returns_flat_dicts_with_ids(self):
        out = _render(
            [_m("1", minutes=0)], group_by="none", show_author=False, include=None
        )
        assert out[0]["id"] == "1"
        assert "timestamp" in out[0]

    def test_conversation_groups_into_channel_bursts(self):
        out = _render(
            [_m("1", minutes=0), _m("2", minutes=2)],
            group_by="conversation",
            show_author=False,
            include=None,
        )
        assert out[0]["channel_id"] == "100"
        assert out[0]["bursts"][0]["count"] == 2

    def test_channel_groups_by_channel(self):
        out = _render(
            [_m("1", minutes=0, channel="100"), _m("2", minutes=0, channel="200")],
            group_by="channel",
            show_author=False,
            include=None,
        )
        assert {c["channel_id"] for c in out} == {"100", "200"}


class TestEventTools:
    def _server(self):
        import asyncio as _asyncio
        from unittest.mock import MagicMock

        from discord_mcp.config import Config
        from discord_mcp.discord.listener import GatewayListener
        from discord_mcp.events import EventBuffer
        from discord_mcp.mcp_server import create_mcp_server

        async def factory():
            await _asyncio.Event().wait()

        listener = GatewayListener(factory, EventBuffer(epoch="e"))  # type: ignore[arg-type]
        return create_mcp_server(MagicMock(), Config.load(), listener), listener

    async def _call(self, server, name: str, **args):
        import json

        result = await server.call_tool(name, args)
        return json.loads(result[0].text)

    async def test_get_events_returns_mentions_and_cursor(self):
        from discord_mcp.models import MessageEvent

        server, listener = self._server()
        msg = Message(
            id="1",
            content="ping",
            author_name="a",
            channel_id=snowflake("42"),
            timestamp=datetime(2026, 11, 16, tzinfo=timezone.utc),
            attachments=[],
        )
        listener.buffer.publish(
            MessageEvent(frozenset({"message", "mention"}), snowflake("7"), msg)
        )
        out = await self._call(server, "get_events")
        assert [e["message"]["id"] for e in out["events"]] == ["1"]
        assert out["events"][0]["kinds"] == ["mention", "message"]
        assert out["cursor"] == "e:1"
        await listener.close()

    async def test_validation_errors(self):
        server, listener = self._server()
        bad_kind = await self._call(server, "get_events", kinds=["nope"])
        bad_cursor = await self._call(server, "get_events", since="zzz")
        bad_timeout = await self._call(server, "wait_for_events", timeout_s=86_401)
        assert bad_kind["error"] == "validation_error"
        assert bad_cursor["error"] == "InvalidCursor"
        assert bad_timeout["error"] == "validation_error"
        await listener.close()

    async def test_wait_for_events_times_out_with_cursor(self):
        server, listener = self._server()
        out = await self._call(server, "wait_for_events", timeout_s=1)
        assert out["events"] == [] and out["timed_out"] is True
        assert out["cursor"] == "e:0"
        await listener.close()
