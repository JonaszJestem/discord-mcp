"""Raw gateway hook, `watch --archive` wiring and the `archive` CLI. No Discord."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from discord_mcp import cli
from discord_mcp.archive import MessageArchive
from discord_mcp.archive_cli import parse_time, run_archive_cli
from discord_mcp.discord.browser_driver import (
    _GatewayState,  # pyright: ignore[reportPrivateUsage]
    _handle_payload,  # pyright: ignore[reportPrivateUsage]
)
from discord_mcp.discord.listener import GatewayListener
from discord_mcp.errors import SessionExpired
from discord_mcp.events import EventBuffer
from discord_mcp.models import MessageEvent
from tests.test_archive import _Server, interrupt_download  # pyright: ignore[reportPrivateUsage]

SELF, OTHER, GUILD, CHANNEL = "100", "200", "300", "400"


def _dispatch(t: str, d: dict[str, Any]) -> dict[str, Any]:
    return {"op": 0, "t": t, "d": d}


READY = _dispatch(
    "READY",
    {
        "user": {"id": SELF, "username": "me"},
        "guilds": [
            {
                "id": GUILD,
                "name": "Team",
                "channels": [{"id": CHANNEL, "name": "general"}],
            }
        ],
    },
)


def _create(mid: str, author: str, content: str, **extra: Any) -> dict[str, Any]:
    return _dispatch(
        "MESSAGE_CREATE",
        {
            "id": mid,
            "channel_id": CHANNEL,
            "guild_id": GUILD,
            "author": {"id": author, "username": f"user{author}"},
            "content": content,
            "timestamp": "2026-11-19T10:00:00+00:00",
            "attachments": [],
            "embeds": [],
            "mentions": [],
            **extra,
        },
    )


class TestRawHook:
    def test_on_raw_sees_every_dispatch_before_filtering(self):
        raw: list[dict[str, Any]] = []
        events: list[MessageEvent] = []
        state = _GatewayState(last_frame=0.0)
        own = _create("1", SELF, "mine")
        theirs = _create("2", OTHER, "hi")
        for payload in (READY, own, {"op": 11}, theirs, _dispatch("TYPING_START", {})):
            _handle_payload(payload, state, events.append, raw.append)
        assert raw == [READY, own, theirs, _dispatch("TYPING_START", {})]
        assert [e.message.id for e in events] == ["2"]

    def test_sink_errors_are_swallowed(self, caplog: pytest.LogCaptureFixture):
        def broken(_: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

        events: list[MessageEvent] = []
        state = _GatewayState(last_frame=0.0)
        _handle_payload(READY, state, events.append, broken)
        _handle_payload(_create("2", OTHER, "hi"), state, events.append, broken)
        assert [e.message.id for e in events] == ["2"]
        assert "sink down" in caplog.text

    def test_without_sink_behaviour_is_unchanged(self):
        events: list[MessageEvent] = []
        state = _GatewayState(last_frame=0.0)
        _handle_payload(READY, state, events.append)
        _handle_payload(_create("2", OTHER, "hi"), state, events.append)
        assert state.self_id == SELF and len(events) == 1


class _ArchivingDriver:
    """Streams a few raw payloads, then the session expires."""

    def __init__(self) -> None:
        self.got_sink = False

    async def stream_gateway(
        self,
        on_event: Callable[[MessageEvent], None],
        *,
        silence_s: float,
        on_raw: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.got_sink = on_raw is not None
        for payload in (
            READY,
            _create("1", SELF, "my own message"),
            _create("2", OTHER, "hello"),
            _dispatch("MESSAGE_DELETE", {"id": "2", "channel_id": CHANNEL}),
        ):
            _handle_payload(payload, _STATE, on_event, on_raw)
        raise SessionExpired("logged out")

    async def close(self) -> None:
        pass


_STATE = _GatewayState(last_frame=0.0)


@pytest.fixture
def watch_rig(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    rig = SimpleNamespace(archive_dir=tmp_path / "archive", drivers=[])
    monkeypatch.setattr(
        cli.Config,
        "load",
        classmethod(lambda c: SimpleNamespace(archive_dir=rig.archive_dir)),
    )
    monkeypatch.setattr(
        cli, "_build_session_store", lambda _: SimpleNamespace(load=lambda: {})
    )

    def build_listener(_session: object, _config: object) -> GatewayListener:
        async def factory() -> _ArchivingDriver:
            driver = _ArchivingDriver()
            rig.drivers.append(driver)
            return driver

        return GatewayListener(factory, EventBuffer())  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_build_listener", build_listener)
    _STATE.self_id = None
    return rig


def _watch(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["discord-mcp", *argv])
    return cli.main()


class TestWatchArchive:
    def test_watch_archive_stores_everything_and_keeps_stdout_filtered(
        self,
        watch_rig: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        assert _watch(monkeypatch, ["watch", "--kinds", "message", "--archive"]) == 1
        out = capsys.readouterr().out
        assert [json.loads(line)["message"]["id"] for line in out.splitlines()] == ["2"]
        assert watch_rig.drivers[0].got_sink

        archive = MessageArchive(watch_rig.archive_dir, resume=False)
        try:
            own, other = archive.get("1"), archive.get("2")
        finally:
            archive.close()
        assert own is not None and own.content == "my own message"
        assert other is not None and other.deleted_at is not None
        assert other.channel_label == "#general" and other.guild_name == "Team"

    def test_watch_without_flag_creates_nothing(
        self,
        watch_rig: SimpleNamespace,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        assert _watch(monkeypatch, ["watch", "--kinds", "message"]) == 1
        assert "hello" in capsys.readouterr().out
        assert not watch_rig.archive_dir.exists()
        assert watch_rig.drivers[0].got_sink is False

    def test_unopenable_archive_does_not_stop_watch(
        self,
        watch_rig: SimpleNamespace,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
    ):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        watch_rig.archive_dir = blocker / "archive"
        assert _watch(monkeypatch, ["watch", "--kinds", "message", "--archive"]) == 1
        out = capsys.readouterr().out
        assert [json.loads(line)["message"]["id"] for line in out.splitlines()] == ["2"]
        assert "archive unavailable" in caplog.text


@pytest.fixture
def server() -> Any:
    s = _Server()
    yield s
    s.httpd.shutdown()


@pytest.fixture
def filled(tmp_path: Path, server: _Server) -> Path:
    url = server.serve("/a.png", (404, b""))
    root = tmp_path / "archive"
    archive = MessageArchive(root)
    archive.handle_dispatch(READY)
    archive.handle_dispatch(
        _create(
            "1",
            OTHER,
            "first draft",
            embeds=[{"title": "Linked page"}],
            attachments=[{"id": "9", "filename": "a.png", "url": url}],
        )
    )
    archive.handle_dispatch(
        _dispatch(
            "MESSAGE_UPDATE",
            {
                "id": "1",
                "channel_id": CHANNEL,
                "content": "final text",
                "edited_timestamp": "2026-11-19T10:05:00+00:00",
            },
        )
    )
    archive.handle_dispatch(_create("2", SELF, "bye", timestamp="2026-11-19T11:00:00Z"))
    archive.handle_dispatch(
        _dispatch("MESSAGE_DELETE", {"id": "2", "channel_id": CHANNEL})
    )
    assert archive.wait_idle(5)
    archive.close()
    return root


def _run(
    argv: list[str], root: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = run_archive_cli(argv, root)
    out, err = capsys.readouterr()
    return code, out, err


class TestArchiveCli:
    def test_search_text_matches_old_versions(
        self, filled: Path, capsys: pytest.CaptureFixture[str]
    ):
        code, out, _ = _run(["search", "--text", "draft"], filled, capsys)
        assert code == 0
        assert "#general (Team)" in out and "user200" in out and "(edited)" in out
        assert "final text" in out and "[embed] Linked page" in out
        assert "[1]" in out and "[2]" not in out

    def test_search_filters_and_json(
        self, filled: Path, capsys: pytest.CaptureFixture[str]
    ):
        code, out, _ = _run(["search", "--deleted", "--json"], filled, capsys)
        rows = [json.loads(line) for line in out.splitlines()]
        assert code == 0 and [r["id"] for r in rows] == ["2"]
        assert rows[0]["deleted_at"] and rows[0]["channel_label"] == "#general"

        _, out, _ = _run(["search", "--edited"], filled, capsys)
        assert "[1]" in out and "[2]" not in out
        _, out, _ = _run(["search", "--author", "user100"], filled, capsys)
        assert "(DELETED)" in out and "[1]" not in out
        _, out, _ = _run(
            [
                "search",
                "--channel",
                "gen",
                "--guild",
                "team",
                "--since",
                "2026-11-19T10:30",
            ],
            filled,
            capsys,
        )
        assert "[2]" in out and "[1]" not in out
        _, out, _ = _run(["search", "--until", "2026-11-19T10:30Z"], filled, capsys)
        assert "[1]" in out and "[2]" not in out
        _, out, _ = _run(["search"], filled, capsys)
        assert out.index("[1]") < out.index("[2]")

    def test_show_prints_history_and_files(
        self, filled: Path, capsys: pytest.CaptureFixture[str]
    ):
        code, out, _ = _run(["show", "1"], filled, capsys)
        assert code == 0
        assert (
            out.index("version 1") < out.index("first draft") < out.index("version 2")
        )
        assert "final text" in out and "a.png" in out
        code, _, err = _run(["show", "999"], filled, capsys)
        assert code == 1 and "not archived" in err

    def test_stats(self, filled: Path, capsys: pytest.CaptureFixture[str]):
        code, out, _ = _run(["stats"], filled, capsys)
        stats = json.loads(out)
        assert code == 0 and stats["messages"] == 2 and stats["deleted"] == 1
        assert stats["edited"] == 1 and stats["guilds"] == 1

    def test_read_commands_do_not_requeue_downloads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        root = tmp_path / "archive"
        interrupt_download(
            root,
            _create(
                "1",
                OTHER,
                "x",
                attachments=[
                    {"id": "9", "filename": "a", "url": "http://127.0.0.1:9/a"}
                ],
            )["d"],
        )
        for argv in (["search"], ["show", "1"], ["stats"]):
            assert _run(argv, root, capsys)[0] == 0
        check = MessageArchive(root, resume=False)
        try:
            assert check.stats().attachments == {"pending": 1}
        finally:
            check.close()

    def test_retry(
        self, filled: Path, server: _Server, capsys: pytest.CaptureFixture[str]
    ):
        server.serve("/a.png", (200, b"png"))
        code, out, _ = _run(["retry"], filled, capsys)
        assert code == 0 and "retried 1" in out and "'done': 1" in out
        _, out, _ = _run(["show", "1"], filled, capsys)
        assert str(filled / "attachments" / CHANNEL / "1_a.png") in out

    def test_bad_since_exits_2(self, filled: Path, capsys: pytest.CaptureFixture[str]):
        code, _, err = _run(["search", "--since", "yesterday"], filled, capsys)
        assert code == 2 and "bad time" in err

    def test_missing_archive_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        code, _, err = _run(["stats"], tmp_path / "nothing", capsys)
        assert code == 1 and "no archive" in err
        assert not (tmp_path / "nothing").exists()

    def test_dispatched_from_main_cli(
        self,
        filled: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        monkeypatch.setattr(
            cli.Config,
            "load",
            classmethod(lambda c: SimpleNamespace(archive_dir=filled)),
        )
        assert cli._dispatch(["archive", "stats"]) == 0
        assert json.loads(capsys.readouterr().out)["messages"] == 2


def test_text_output_strips_terminal_escapes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    root = tmp_path / "archive"
    archive = MessageArchive(root)
    archive.handle_dispatch(
        _create("1", OTHER, "hi\x1b]0;pwned\x07\x1b[2Jthere\nline 2")
    )
    archive.close()
    code, out, _ = _run(["search"], root, capsys)
    assert code == 0 and "\x1b" not in out and "\x07" not in out
    assert "hi]0;pwned[2Jthere" in out and "    line 2" in out


def test_parse_time_relative_and_iso():
    from datetime import UTC, datetime

    now = datetime(2026, 11, 19, 12, tzinfo=UTC)
    assert parse_time("24h", now=now) == "2026-11-18T12:00:00.000000Z"
    assert parse_time("7d", now=now) == "2026-11-12T12:00:00.000000Z"
    assert parse_time("2026-11-19T10:00+01:00") == "2026-11-19T09:00:00.000000Z"
    assert parse_time("2026-11-19", end_of_day=True) == "2026-11-19T23:59:59.999999Z"
    with pytest.raises(ValueError):
        parse_time("soon")
