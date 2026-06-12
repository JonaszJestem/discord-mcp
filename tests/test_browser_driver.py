"""Tests for the pure search-parsing helpers in browser_driver."""

from datetime import datetime, timezone

from discord_mcp.discord.browser_driver import (
    _author_name_id,
    _mention_names_from_api,
    _message_from_api,
    _reply_ref_from_api,
    _search_groups,
    _search_hit,
    _thread_last_activity,
)
from discord_mcp.models import snowflake, snowflake_for_time


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

    def test_parses_reply_reference(self):
        raw = {
            "id": "2",
            "content": "ah, I meant the query fix",
            "author": {"id": "10", "username": "maiko"},
            "referenced_message": {
                "id": "1",
                "content": "wait dont merge",
                "author": {"id": "20", "global_name": "Daniel C"},
            },
        }
        msg = _message_from_api(raw, snowflake("100"))
        assert msg.reply_to is not None
        assert msg.reply_to.author_name == "Daniel C"
        assert msg.reply_to.author_id == "20"
        assert msg.reply_to.content == "wait dont merge"

    def test_parses_mentions(self):
        raw = {
            "id": "1",
            "content": "hey",
            "author": {"id": "10", "username": "x"},
            "mentions": [
                {"id": "20", "global_name": "Daniel C"},
                {"id": "30", "username": "eli"},
            ],
        }
        assert _message_from_api(raw, snowflake("100")).mention_names == [
            "Daniel C",
            "eli",
        ]


class TestAuthorNameId:
    def test_prefers_global_name(self):
        assert _author_name_id({"id": "9", "global_name": "Bob", "username": "b"}) == (
            "Bob",
            "9",
        )

    def test_non_numeric_id_dropped(self):
        assert _author_name_id({"id": "abc", "username": "b"}) == ("b", None)

    def test_non_dict_is_unknown(self):
        assert _author_name_id(None) == ("Unknown", None)


class TestReplyRefFromApi:
    def test_none_when_no_reference(self):
        assert _reply_ref_from_api(None) is None

    def test_handles_missing_author(self):
        ref = _reply_ref_from_api({"id": "1", "content": "x"})
        assert ref is not None
        assert ref.author_name == "Unknown"


class TestMentionNamesFromApi:
    def test_empty_for_non_list(self):
        assert _mention_names_from_api(None) == []

    def test_skips_unnamed_entries(self):
        assert _mention_names_from_api([{"id": "1"}, {"username": "a"}]) == ["a"]


class TestThreadLastActivity:
    def test_derives_from_last_message_id(self):
        t = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
        sf = snowflake_for_time(t)
        assert _thread_last_activity({"last_message_id": sf}) == t

    def test_falls_back_to_archive_timestamp(self):
        got = _thread_last_activity(
            {"thread_metadata": {"archive_timestamp": "2026-06-10T12:00:00+00:00"}}
        )
        assert got == datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)

    def test_none_when_nothing_present(self):
        assert _thread_last_activity({}) is None
