"""Tests for the non-Playwright logic in mcp_server: chunking + error formatting."""

from datetime import datetime, timezone

from discord_mcp.errors import InvalidSnowflake, SessionExpired
from discord_mcp.mcp_server import _chunk_message, _cutoff, _to_error


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


class TestCutoff:
    def test_none_means_no_lower_bound(self):
        assert _cutoff(None) is None

    def test_returns_past_utc_instant(self):
        before = datetime.now(timezone.utc)
        cutoff = _cutoff(24)
        assert cutoff is not None
        assert cutoff.tzinfo is not None
        assert cutoff < before
