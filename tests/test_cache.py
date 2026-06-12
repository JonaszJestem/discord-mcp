from datetime import datetime, timezone

from discord_mcp.cache import DiscoveryCache
from discord_mcp.models import Channel, Guild, Message, snowflake


def _msg(author_id: str | None, name: str, ts: datetime) -> Message:
    return Message(
        id="1",
        content="hi",
        author_name=name,
        channel_id=snowflake("10"),
        timestamp=ts,
        attachments=[],
        author_id=author_id,
    )


class TestResolvePerson:
    def test_numeric_input_passes_through(self, tmp_path):
        cache = DiscoveryCache(tmp_path / "c.json")
        assert cache.resolve_person("125570309171445760") == "125570309171445760"

    def test_resolves_known_handle_case_insensitively(self, tmp_path):
        cache = DiscoveryCache(tmp_path / "c.json")
        cache.record_people([_msg("999", "BobTheShoplifter", _now())])
        assert cache.resolve_person("bobtheshoplifter") == "999"

    def test_strips_leading_at(self, tmp_path):
        cache = DiscoveryCache(tmp_path / "c.json")
        cache.record_people([_msg("999", "bob", _now())])
        assert cache.resolve_person("@bob") == "999"

    def test_unknown_handle_returns_none(self, tmp_path):
        cache = DiscoveryCache(tmp_path / "c.json")
        assert cache.resolve_person("ghost") is None


class TestRecord:
    def test_only_records_authors_with_id(self, tmp_path):
        cache = DiscoveryCache(tmp_path / "c.json")
        cache.record_people([_msg(None, "anon", _now())])
        assert cache.known_people() == []

    def test_keeps_most_recent_last_seen(self, tmp_path):
        cache = DiscoveryCache(tmp_path / "c.json")
        older = datetime(2020, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2024, 1, 1, tzinfo=timezone.utc)
        cache.record_people([_msg("7", "bob", newer)])
        cache.record_people([_msg("7", "bob", older)])
        person = cache.known_people()[0]
        assert person["last_seen"] == newer.isoformat()

    def test_records_servers_and_channels(self, tmp_path):
        cache = DiscoveryCache(tmp_path / "c.json")
        cache.record_guilds([Guild(id=snowflake("1"), name="Server")])
        cache.record_channels(
            [Channel(id=snowflake("2"), name="general", type=0, guild_id=snowflake("1"))]
        )
        reloaded = DiscoveryCache(tmp_path / "c.json")
        assert reloaded.resolve_person("123") == "123"  # cache usable after reload


class TestPersistence:
    def test_round_trips_across_instances(self, tmp_path):
        path = tmp_path / "c.json"
        DiscoveryCache(path).record_people([_msg("42", "alice", _now())])
        assert DiscoveryCache(path).resolve_person("alice") == "42"

    def test_corrupt_file_is_tolerated(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not valid json")
        cache = DiscoveryCache(path)  # must not raise
        assert cache.known_people() == []

    def test_missing_file_is_empty(self, tmp_path):
        assert DiscoveryCache(tmp_path / "nope.json").known_people() == []


def _now() -> datetime:
    return datetime(2023, 6, 1, tzinfo=timezone.utc)
