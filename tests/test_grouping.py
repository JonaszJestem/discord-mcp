"""Tests for the pure grouping/serialisation helpers."""

from datetime import datetime, timedelta, timezone

from discord_mcp.grouping import (
    group_channels,
    group_conversations,
    message_view,
    split_bursts,
)
from discord_mcp.models import Message, ReplyRef, snowflake

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _msg(
    mid: str,
    *,
    minutes: int,
    channel: str = "100",
    content: str = "hi",
    author: str = "Bob",
    reply_to: ReplyRef | None = None,
    mentions: list[str] | None = None,
    attachments: list[str] | None = None,
) -> Message:
    return Message(
        id=mid,
        content=content,
        author_name=author,
        channel_id=snowflake(channel),
        timestamp=BASE + timedelta(minutes=minutes),
        attachments=attachments or [],
        author_id="1",
        reply_to=reply_to,
        mention_names=mentions or [],
    )


class TestMessageView:
    def test_minimal_view_has_time_and_content(self):
        v = message_view(
            _msg("1", minutes=0), show_author=False, include_mentions=False
        )
        assert v == {"t": "14:00", "c": "hi"}

    def test_author_shown_only_when_requested(self):
        v = message_view(_msg("1", minutes=0), show_author=True, include_mentions=False)
        assert v["by"] == "Bob"

    def test_reply_context_attached(self):
        reply = ReplyRef(
            message_id="9", author_name="Daniel", author_id="2", content="wait dont merge"
        )
        v = message_view(
            _msg("1", minutes=0, reply_to=reply),
            show_author=False,
            include_mentions=False,
        )
        assert v["re"] == {"who": "Daniel", "c": "wait dont merge"}

    def test_reply_content_truncated_and_flattened(self):
        reply = ReplyRef(
            message_id="9",
            author_name="X",
            author_id=None,
            content="line1\n" + "z" * 300,
        )
        v = message_view(
            _msg("1", minutes=0, reply_to=reply),
            show_author=False,
            include_mentions=False,
        )
        assert "\n" not in v["re"]["c"]
        assert v["re"]["c"].endswith("…")
        assert len(v["re"]["c"]) <= 140

    def test_mentions_only_when_included(self):
        m = _msg("1", minutes=0, mentions=["Alice"])
        assert "@" not in message_view(m, show_author=False, include_mentions=False)
        assert message_view(m, show_author=False, include_mentions=True)["@"] == [
            "Alice"
        ]

    def test_attachments_collapsed_to_count(self):
        m = _msg("1", minutes=0, attachments=["a", "b"])
        v = message_view(m, show_author=False, include_mentions=False)
        assert v["attachments"] == 2


class TestSplitBursts:
    def test_single_burst_when_close(self):
        bursts = split_bursts([_msg("1", minutes=0), _msg("2", minutes=3)])
        assert len(bursts) == 1

    def test_breaks_on_large_gap(self):
        bursts = split_bursts([_msg("1", minutes=0), _msg("2", minutes=30)])
        assert [len(b) for b in bursts] == [1, 1]

    def test_orders_unsorted_input_chronologically(self):
        bursts = split_bursts([_msg("2", minutes=3), _msg("1", minutes=0)])
        assert [m.id for m in bursts[0]] == ["1", "2"]

    def test_empty_input(self):
        assert split_bursts([]) == []


class TestGroupConversations:
    def test_splits_on_time_gap(self):
        msgs = [
            _msg("1", minutes=0),
            _msg("2", minutes=2),
            _msg("3", minutes=40),  # > 10 min gap → new burst
        ]
        out = group_conversations(msgs, show_author=False, include_mentions=False)
        assert len(out) == 1  # one channel
        bursts = out[0]["bursts"]
        assert [b["count"] for b in bursts] == [1, 2]  # newest burst first

    def test_messages_within_burst_are_chronological(self):
        msgs = [_msg("2", minutes=3), _msg("1", minutes=0)]
        out = group_conversations(msgs, show_author=False, include_mentions=False)
        contents = [m["t"] for m in out[0]["bursts"][0]["messages"]]
        assert contents == ["14:00", "14:03"]

    def test_channels_ordered_by_latest_activity(self):
        msgs = [
            _msg("1", minutes=0, channel="100"),
            _msg("2", minutes=50, channel="200"),
        ]
        out = group_conversations(msgs, show_author=False, include_mentions=False)
        assert out[0]["channel_id"] == "200"  # more recent channel first

    def test_span_format_same_day(self):
        msgs = [_msg("1", minutes=0), _msg("2", minutes=5)]
        out = group_conversations(msgs, show_author=False, include_mentions=False)
        assert out[0]["bursts"][0]["span"] == "Jun 12 14:00–14:05"


class TestGroupChannels:
    def test_one_bucket_per_channel_newest_first(self):
        msgs = [
            _msg("1", minutes=0, channel="100"),
            _msg("2", minutes=5, channel="100"),
            _msg("3", minutes=1, channel="200"),
        ]
        out = group_channels(msgs, show_author=False, include_mentions=False)
        assert len(out) == 2
        c100 = next(c for c in out if c["channel_id"] == "100")
        assert c100["count"] == 2
        assert [m["t"] for m in c100["messages"]] == ["14:05", "14:00"]
