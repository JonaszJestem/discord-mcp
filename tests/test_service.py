"""Tests for pure helpers in the service layer."""

from datetime import datetime, timedelta, timezone

from discord_mcp.discord.service import _merge_dedupe, _merge_with_context
from discord_mcp.models import Message, snowflake

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _msg(mid: str, *, minutes: int) -> Message:
    return Message(
        id=mid,
        content="x",
        author_name="A",
        channel_id=snowflake("100"),
        timestamp=BASE + timedelta(minutes=minutes),
        attachments=[],
        author_id="1",
    )


class TestMergeDedupe:
    def test_dedupes_by_id(self):
        out = _merge_dedupe(
            [_msg("1", minutes=0)], [_msg("1", minutes=0)], limit=10
        )
        assert len(out) == 1

    def test_orders_newest_first(self):
        out = _merge_dedupe(
            [_msg("1", minutes=0)], [_msg("2", minutes=5)], limit=10
        )
        assert [m.id for m in out] == ["2", "1"]

    def test_respects_limit(self):
        out = _merge_dedupe(
            [_msg("1", minutes=0), _msg("2", minutes=1)],
            [_msg("3", minutes=2)],
            limit=2,
        )
        assert len(out) == 2
        assert out[0].id == "3"  # newest kept

    def test_merges_thread_only_messages(self):
        out = _merge_dedupe(
            [_msg("1", minutes=0)], [_msg("2", minutes=1)], limit=10
        )
        assert {m.id for m in out} == {"1", "2"}


class TestMergeWithContext:
    def test_keeps_all_focus_messages(self):
        focus = [_msg("1", minutes=0), _msg("2", minutes=100)]
        context = [_msg(f"c{i}", minutes=i) for i in range(50)]
        out = _merge_with_context(focus, context, budget=5)
        ids = {m.id for m in out}
        assert {"1", "2"} <= ids  # focus never dropped

    def test_caps_context_to_budget(self):
        focus = [_msg("1", minutes=0)]
        context = [_msg(f"c{i}", minutes=i + 1) for i in range(50)]
        out = _merge_with_context(focus, context, budget=5)
        assert len(out) == 1 + 5

    def test_dedupes_focus_from_context(self):
        focus = [_msg("1", minutes=0)]
        out = _merge_with_context(focus, [_msg("1", minutes=0)], budget=5)
        assert len(out) == 1

    def test_result_is_newest_first(self):
        focus = [_msg("1", minutes=0)]
        context = [_msg("2", minutes=5)]
        out = _merge_with_context(focus, context, budget=5)
        assert [m.id for m in out] == ["2", "1"]
