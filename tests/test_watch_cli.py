"""`watch` and `serve` wiring of the gateway listener. No Discord."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from discord_mcp import cli
from discord_mcp.discord.listener import GatewayListener
from discord_mcp.errors import SessionExpired
from discord_mcp.events import EventBuffer
from discord_mcp.models import EventKind, Message, MessageEvent, snowflake


def _event(mid: str, *kinds: EventKind) -> MessageEvent:
    return MessageEvent(
        frozenset({"message", *kinds}),
        snowflake("7"),
        Message(
            id=mid,
            content="hey",
            author_name="a",
            channel_id=snowflake("42"),
            timestamp=datetime(2026, 11, 16, tzinfo=UTC),
            attachments=[],
        ),
    )


class _ExpiringDriver:
    async def stream_gateway(
        self, on_event: Callable[[MessageEvent], None], *, silence_s: float
    ) -> None:
        on_event(_event("noise"))
        on_event(_event("ping", "mention"))
        raise SessionExpired("logged out")

    async def close(self) -> None:
        pass


@pytest.fixture
def wiring(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    seen = SimpleNamespace(built=[])
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda c: SimpleNamespace()))
    monkeypatch.setattr(
        cli, "_build_session_store", lambda _: SimpleNamespace(load=lambda: {})
    )

    def build_listener(_session: object, _config: object) -> GatewayListener:
        async def factory() -> _ExpiringDriver:
            return _ExpiringDriver()

        listener = GatewayListener(factory, EventBuffer())  # type: ignore[arg-type]
        seen.built.append(listener)
        return listener

    monkeypatch.setattr(cli, "_build_listener", build_listener)
    return seen


def test_watch_prints_matching_events_then_exits_on_expired_session(
    wiring: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.setattr("sys.argv", ["discord-mcp", "watch", "--kinds", "mention"])
    assert cli.main() == 1
    out, err = capsys.readouterr()
    lines = [json.loads(line) for line in out.splitlines()]
    assert [e["message"]["id"] for e in lines] == ["ping"]
    assert lines[0]["kinds"] == ["mention", "message"]
    assert "logged out" in err


def test_watch_rejects_unknown_kind(
    wiring: SimpleNamespace, capsys: pytest.CaptureFixture[str]
):
    assert cli._cmd_watch(["--kinds", "mention,bogus"]) == 2
    assert "bogus" in capsys.readouterr().err


def test_serve_hands_its_listener_to_the_mcp_server(
    wiring: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    captured: list[object] = []

    def fake_create(_service: object, _config: object, listener: object):
        captured.append(listener)
        return SimpleNamespace(run=lambda: None)

    monkeypatch.setattr(
        cli.Config,
        "load",
        classmethod(
            lambda c: SimpleNamespace(
                headless=True, pool_size=1, cache_path=tmp_path / "cache.json"
            )
        ),
    )
    monkeypatch.setattr(cli, "create_mcp_server", fake_create)
    assert cli._cmd_serve([]) == 0
    assert captured == wiring.built and len(captured) == 1
