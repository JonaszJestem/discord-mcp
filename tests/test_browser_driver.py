"""Tests for the pure search-parsing helpers in browser_driver."""

from discord_mcp.discord.browser_driver import (
    _message_from_api,
    _search_groups,
    _search_hit,
)
from discord_mcp.models import snowflake


class TestSearchGroups:
    def test_extracts_messages_list(self):
        payload = {"messages": [[{"id": "1"}], [{"id": "2"}]], "total_results": 2}
        assert len(_search_groups(payload)) == 2

    def test_non_dict_payload_is_empty(self):
        assert _search_groups(None) == []
        assert _search_groups("nope") == []

    def test_missing_messages_key_is_empty(self):
        assert _search_groups({"total_results": 0}) == []


class TestSearchHit:
    def test_prefers_hit_flagged_item(self):
        group = [{"id": "ctx"}, {"id": "real", "hit": True}]
        assert _search_hit(group) == {"id": "real", "hit": True}

    def test_falls_back_to_first_item(self):
        group = [{"id": "first"}, {"id": "second"}]
        assert _search_hit(group) == {"id": "first"}

    def test_non_list_group_is_none(self):
        assert _search_hit({"id": "x"}) is None

    def test_empty_group_is_none(self):
        assert _search_hit([]) is None


class TestMessageFromApi:
    def test_captures_author_id_and_name(self):
        raw = {
            "id": "100",
            "content": "hi",
            "channel_id": "200",
            "timestamp": "2026-06-12T11:00:00.000000+00:00",
            "author": {"id": "125570309171445760", "username": "bob"},
        }
        msg = _message_from_api(raw, snowflake("200"))
        assert msg.author_id == "125570309171445760"
        assert msg.author_name == "bob"

    def test_prefers_global_name_over_username(self):
        raw = {"id": "1", "author": {"id": "9", "global_name": "Bob", "username": "bob"}}
        assert _message_from_api(raw, snowflake("1")).author_name == "Bob"

    def test_non_numeric_author_id_dropped(self):
        raw = {"id": "1", "author": {"id": "not-a-snowflake", "username": "x"}}
        assert _message_from_api(raw, snowflake("1")).author_id is None

    def test_missing_author_defaults(self):
        msg = _message_from_api({"id": "1", "content": "x"}, snowflake("1"))
        assert msg.author_id is None
        assert msg.author_name == "Unknown"
