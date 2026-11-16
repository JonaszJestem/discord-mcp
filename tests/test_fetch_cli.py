"""The fetch CLI: argument parsing, dispatch and JSON shape. No Discord."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from discord_mcp import cli
from discord_mcp.errors import InvalidSnowflake
from discord_mcp.models import Channel, Guild, Message, ReplyRef, Thread, snowflake
from discord_mcp.serialization import message_to_dict

GUILD = "1105327337555378246"
CHANNEL = "1238847425343393854"


def _message(**over: object) -> Message:
    base: dict[str, object] = {
        "id": "999",
        "content": "take another look if you can",
        "author_name": "JoshuaSingh",
        "channel_id": snowflake(CHANNEL),
        "timestamp": datetime(2026, 10, 5, 9, 33, tzinfo=UTC),
        "attachments": [],
        "author_id": "834862073766871072",
    }
    base.update(over)
    return Message(**base)  # type: ignore[arg-type]


class FakeService:
    def __init__(self) -> None:
        self.closed = False
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    async def close(self) -> None:
        self.closed = True

    async def list_guilds(self) -> list[Guild]:
        return [Guild(id=snowflake(GUILD), name="Rainbet Internal")]

    async def list_channels(self, guild_id: str) -> list[Channel]:
        self.calls.append(("list_channels", (guild_id,), {}))
        return [
            Channel(
                id=snowflake(CHANNEL),
                name="frontend-development",
                type=0,
                guild_id=snowflake(GUILD),
            )
        ]

    async def list_threads(self, guild_id: str, channel_id: str) -> list[Thread]:
        self.calls.append(("list_threads", (guild_id, channel_id), {}))
        return [
            Thread(
                id=snowflake("1554105211403112468"),
                name="halloween categories",
                parent_channel_id=snowflake(CHANNEL),
            )
        ]

    async def list_mentions(self, *, limit: int) -> list[Message]:
        self.calls.append(("list_mentions", (), {"limit": limit}))
        return [_message()]

    async def read_recent_messages(
        self, guild_id: str, channel_id: str, *, hours_back: int, max_messages: int
    ) -> list[Message]:
        self.calls.append(
            (
                "read_recent_messages",
                (guild_id, channel_id),
                {"hours_back": hours_back, "max_messages": max_messages},
            )
        )
        return [_message()]

    async def read_thread(
        self, guild_id: str, thread_id: str, *, hours_back: int, max_messages: int
    ) -> list[Message]:
        self.calls.append(
            (
                "read_thread",
                (guild_id, thread_id),
                {"hours_back": hours_back, "max_messages": max_messages},
            )
        )
        return [_message()]


async def _run(argv: list[str]) -> tuple[dict[str, object], FakeService]:
    service = FakeService()
    ns = cli._fetch_parser().parse_args(argv)
    payload = await cli._fetch(service, ns)  # type: ignore[arg-type]
    return payload, service


async def test_guilds_payload_shape() -> None:
    payload, _ = await _run(["guilds"])
    assert payload["kind"] == "guilds"
    assert payload["count"] == 1
    assert payload["items"] == [{"id": GUILD, "name": "Rainbet Internal"}]


async def test_mentions_passes_limit_through() -> None:
    payload, service = await _run(["mentions", "--limit", "7"])
    assert service.calls == [("list_mentions", (), {"limit": 7})]
    assert payload["count"] == 1


async def test_channel_read_defaults_and_overrides() -> None:
    _, service = await _run(["channel", "--guild", GUILD, "--channel", CHANNEL])
    assert service.calls[0][2] == {"hours_back": 24, "max_messages": 200}

    _, service = await _run(
        [
            "channel",
            "--guild",
            GUILD,
            "--channel",
            CHANNEL,
            "--hours",
            "2",
            "--limit",
            "50",
        ]
    )
    assert service.calls[0][2] == {"hours_back": 2, "max_messages": 50}


async def test_thread_read_uses_the_thread_path() -> None:
    _, service = await _run(["thread", "--guild", GUILD, "--thread", CHANNEL])
    assert service.calls[0][0] == "read_thread"


async def test_threads_listing() -> None:
    payload, service = await _run(["threads", "--guild", GUILD, "--channel", CHANNEL])
    assert service.calls[0][0] == "list_threads"
    assert payload["items"][0]["name"] == "halloween categories"  # type: ignore[index]


async def test_non_numeric_id_is_rejected_at_the_edge() -> None:
    with pytest.raises(InvalidSnowflake):
        await _run(["channels", "--guild", "../../etc/passwd"])


def test_missing_kind_exits_rather_than_guessing() -> None:
    with pytest.raises(SystemExit):
        cli._fetch_parser().parse_args([])


def test_dispatch_routes_fetch_with_remaining_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(cli, "_cmd_fetch", lambda argv: seen.append(argv) or 0)
    assert cli._dispatch(["fetch", "mentions", "--limit", "3"]) == 0
    assert seen == [["mentions", "--limit", "3"]]


def test_unknown_command_still_prints_usage() -> None:
    assert cli._dispatch(["nope"]) == 2


def test_message_serialisation_keeps_reply_context() -> None:
    msg = _message(
        reply_to=ReplyRef(
            message_id="998",
            author_name="Jonasz",
            author_id="412700413951868928",
            content="fixed",
        ),
        mention_names=["Jonasz"],
        attachments=["https://cdn.example/x.png"],
    )
    out = message_to_dict(msg)
    assert out["timestamp"] == "2026-10-05T09:33:00+00:00"
    assert out["reply_to"]["author_id"] == "412700413951868928"  # type: ignore[index]
    assert out["mention_names"] == ["Jonasz"]
    assert out["attachments"] == ["https://cdn.example/x.png"]
    assert json.dumps(out)


def test_message_serialisation_without_reply() -> None:
    assert message_to_dict(_message())["reply_to"] is None


def test_naive_timestamps_are_read_as_utc() -> None:
    """A local-offset guess here mis-ages every message for the poller."""
    naive = _message(timestamp=datetime(2026, 10, 5, 9, 33))
    assert message_to_dict(naive)["timestamp"] == "2026-10-05T09:33:00+00:00"
