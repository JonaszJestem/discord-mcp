from datetime import datetime, timezone

import pytest

from discord_mcp.errors import InvalidSnowflake
from discord_mcp.models import (
    DISCORD_EPOCH_MS,
    Channel,
    Guild,
    Message,
    ReplyRef,
    Thread,
    snowflake,
    snowflake_for_time,
    time_for_snowflake,
)


class TestSnowflake:
    def test_accepts_numeric_string(self):
        assert snowflake("123456789") == "123456789"

    def test_accepts_very_long_numeric(self):
        assert snowflake("1" * 30) == "1" * 30

    @pytest.mark.parametrize(
        "bad",
        ["", "abc", "123a", " 123", "123 ", "123.45", "-123", "1,2,3"],
    )
    def test_rejects_non_numeric(self, bad: str):
        with pytest.raises(InvalidSnowflake):
            snowflake(bad)

    def test_error_mentions_field_name(self):
        with pytest.raises(InvalidSnowflake, match="channel_id"):
            snowflake("abc", field="channel_id")


class TestModels:
    def test_guild_is_frozen(self):
        g = Guild(id=snowflake("1"), name="x")
        with pytest.raises(Exception):
            g.name = "y"  # type: ignore[misc]

    def test_message_holds_utc_timestamp(self):
        m = Message(
            id="1",
            content="hi",
            author_name="me",
            channel_id=snowflake("1"),
            timestamp=datetime.now(timezone.utc),
            attachments=[],
        )
        assert m.timestamp.tzinfo is not None

    def test_channel_includes_guild_id(self):
        c = Channel(
            id=snowflake("1"),
            name="general",
            type=0,
            guild_id=snowflake("99"),
        )
        assert c.guild_id == "99"

    def test_message_author_id_defaults_none(self):
        m = Message(
            id="1",
            content="hi",
            author_name="me",
            channel_id=snowflake("1"),
            timestamp=datetime.now(timezone.utc),
            attachments=[],
        )
        assert m.author_id is None


class TestSnowflakeForTime:
    def test_discord_epoch_maps_to_zero(self):
        epoch = datetime.fromtimestamp(DISCORD_EPOCH_MS / 1000, tz=timezone.utc)
        assert snowflake_for_time(epoch) == "0"

    def test_pre_epoch_clamps_to_zero(self):
        assert snowflake_for_time(datetime(2000, 1, 1, tzinfo=timezone.utc)) == "0"

    def test_is_monotonic_in_time(self):
        earlier = datetime(2023, 1, 1, tzinfo=timezone.utc)
        later = datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert int(snowflake_for_time(later)) > int(snowflake_for_time(earlier))

    def test_timestamp_bits_match_discord_layout(self):
        # one second after the epoch → 1000 ms → 1000 << 22
        one_sec = datetime.fromtimestamp(
            DISCORD_EPOCH_MS / 1000 + 1, tz=timezone.utc
        )
        assert snowflake_for_time(one_sec) == str(1000 << 22)


class TestTimeForSnowflake:
    def test_inverts_snowflake_for_time(self):
        t = datetime(2026, 6, 12, 14, 30, 15, tzinfo=timezone.utc)
        # snowflake_for_time truncates to whole ms; round-trip should match.
        assert time_for_snowflake(snowflake_for_time(t)) == t

    def test_epoch_snowflake_maps_to_epoch(self):
        epoch = datetime.fromtimestamp(DISCORD_EPOCH_MS / 1000, tz=timezone.utc)
        assert time_for_snowflake("0") == epoch


class TestReplyAndMentions:
    def test_message_defaults_have_no_reply_or_mentions(self):
        m = Message(
            id="1",
            content="x",
            author_name="A",
            channel_id=snowflake("100"),
            timestamp=datetime.now(timezone.utc),
            attachments=[],
        )
        assert m.reply_to is None
        assert m.mention_names == []

    def test_distinct_messages_have_independent_mention_lists(self):
        a = Message(
            id="1",
            content="x",
            author_name="A",
            channel_id=snowflake("100"),
            timestamp=datetime.now(timezone.utc),
            attachments=[],
        )
        b = Message(
            id="2",
            content="y",
            author_name="B",
            channel_id=snowflake("100"),
            timestamp=datetime.now(timezone.utc),
            attachments=[],
        )
        a.mention_names.append("Alice")
        assert b.mention_names == []  # no shared mutable default

    def test_reply_ref_fields(self):
        r = ReplyRef(
            message_id="9", author_name="Daniel", author_id="2", content="wait"
        )
        assert (r.author_name, r.content) == ("Daniel", "wait")


class TestThread:
    def test_last_activity_defaults_none(self):
        t = Thread(
            id=snowflake("1"), name="t", parent_channel_id=snowflake("2")
        )
        assert t.last_activity is None
