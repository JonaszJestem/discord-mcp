"""Gateway frame decoding and MESSAGE_CREATE classification."""

import json
import zlib

import pytest

from discord_mcp.discord.browser_driver import _GatewayState, _handle_payload
from discord_mcp.discord.gateway import ZlibStreamDecoder, classify
from discord_mcp.models import MessageEvent

SELF = "412700413951868928"


def _stream_frames(*payloads: dict[str, object]) -> list[bytes]:
    z = zlib.compressobj()
    return [
        z.compress(json.dumps(p).encode()) + z.flush(zlib.Z_SYNC_FLUSH)
        for p in payloads
    ]


def _msg(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "1500000000000000001",
        "channel_id": "42",
        "guild_id": "7",
        "content": "hi",
        "timestamp": "2026-11-16T10:00:00+00:00",
        "author": {"id": "99", "username": "bob"},
        "mentions": [],
        "attachments": [],
    }
    base.update(over)
    return base


class TestZlibStreamDecoder:
    def test_decodes_consecutive_payloads_sharing_one_context(self):
        frames = _stream_frames({"op": 11}, {"op": 0, "t": "X", "d": {}})
        decoder = ZlibStreamDecoder()
        assert decoder.feed(frames[0]) == {"op": 11}
        assert decoder.feed(frames[1]) == {"op": 0, "t": "X", "d": {}}

    def test_waits_for_sync_flush_suffix_across_chunks(self):
        (frame,) = _stream_frames({"op": 0, "t": "BIG", "d": {"x": "y" * 5000}})
        decoder = ZlibStreamDecoder()
        assert decoder.feed(frame[:10]) is None
        assert decoder.feed(frame[10:])["t"] == "BIG"  # type: ignore[index]

    def test_fresh_decoder_cannot_continue_another_sockets_stream(self):
        frames = _stream_frames({"op": 11}, {"op": 11})
        with pytest.raises(zlib.error):
            ZlibStreamDecoder().feed(frames[1])


class TestClassify:
    def test_plain_guild_message(self):
        assert classify(_msg(), SELF) == {"message"}

    def test_dm_has_no_guild_id(self):
        data = _msg()
        del data["guild_id"]
        assert classify(data, SELF) == {"message", "dm"}

    def test_direct_mention(self):
        data = _msg(mentions=[{"id": SELF}])
        assert classify(data, SELF) == {"message", "mention"}

    def test_everyone_is_not_a_mention(self):
        assert classify(_msg(mention_everyone=True), SELF) == {"message"}

    def test_own_message_is_dropped(self):
        assert classify(_msg(author={"id": SELF}), SELF) is None

    def test_unknown_self_never_flags_mention(self):
        assert classify(_msg(mentions=[{"id": SELF}]), None) == {"message"}


class TestHandlePayload:
    def _run(self, *payloads: dict[str, object]) -> list[MessageEvent]:
        state = _GatewayState(last_frame=0.0)
        out: list[MessageEvent] = []
        for p in payloads:
            _handle_payload(p, state, out.append)
        return out

    def test_ready_sets_self_id_used_for_later_mentions(self):
        events = self._run(
            {"op": 0, "t": "READY", "d": {"user": {"id": SELF, "username": "me"}}},
            {"op": 0, "t": "MESSAGE_CREATE", "d": _msg(mentions=[{"id": SELF}])},
            {"op": 0, "t": "MESSAGE_CREATE", "d": _msg(author={"id": SELF})},
        )
        assert len(events) == 1
        assert events[0].kinds == {"message", "mention"}
        assert events[0].guild_id == "7"
        assert events[0].message.channel_id == "42"
        assert events[0].message.author_id == "99"

    def test_ignores_non_dispatch_and_other_events(self):
        assert (
            self._run(
                {"op": 11},
                {"op": 0, "t": "PRESENCE_UPDATE", "d": {}},
                {"op": 0, "t": "MESSAGE_CREATE", "d": None},
            )
            == []
        )

    def test_messages_before_ready_are_dropped(self):
        events = self._run(
            {"op": 0, "t": "MESSAGE_CREATE", "d": _msg(author={"id": SELF})},
        )
        assert events == []

    def test_message_without_id_is_dropped(self):
        data = _msg()
        del data["id"]
        events = self._run(
            {"op": 0, "t": "READY", "d": {"user": {"id": SELF}}},
            {"op": 0, "t": "MESSAGE_CREATE", "d": data},
        )
        assert events == []
