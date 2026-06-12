from datetime import datetime, timezone

import pytest

from discord_mcp.errors import InvalidSnowflake
from discord_mcp.models import (
    DISCORD_EPOCH_MS,
    Channel,
    Guild,
    Message,
    snowflake,
    snowflake_for_time,
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
