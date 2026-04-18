from datetime import datetime, timezone

import pytest

from discord_mcp.errors import InvalidSnowflake
from discord_mcp.models import Channel, Guild, Message, snowflake


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
