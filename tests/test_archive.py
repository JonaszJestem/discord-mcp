"""MessageArchive against fake gateway payloads and a local HTTP server. No Discord."""

from __future__ import annotations

import http.server
import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from discord_mcp.archive import (
    FetchError,
    MessageArchive,
    SearchQuery,
    normalize_timestamp,
)

SELF = "100"
TIAGO = "200"
GUILD = "300"
GENERAL = "400"
DM = "500"


class _Server:
    def __init__(self) -> None:
        self.routes: dict[str, list[tuple[int, bytes]]] = {}
        self.hits: dict[str, int] = {}
        self.gate: dict[str, threading.Event] = {}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.hits[self.path] = server.hits.get(self.path, 0) + 1
                if self.path in server.gate:
                    server.gate[self.path].wait(5)
                queue = server.routes.get(self.path) or [(404, b"")]
                code, body = queue.pop(0) if len(queue) > 1 else queue[0]
                self.send_response(code)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(
            target=self.httpd.serve_forever, args=(0.05,), daemon=True
        ).start()

    def serve(self, path: str, *responses: tuple[int, bytes]) -> str:
        self.routes[path] = list(responses)
        return self.base + path


@pytest.fixture
def server() -> Iterator[_Server]:
    s = _Server()
    yield s
    s.httpd.shutdown()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


@pytest.fixture
def archive(root: Path) -> Iterator[MessageArchive]:
    a = MessageArchive(root, retry_sleep=lambda _: None)
    yield a
    a.close()


def _ready(a: MessageArchive) -> None:
    a.handle_dispatch(
        {
            "op": 0,
            "t": "READY",
            "d": {
                "user": {"id": SELF, "username": "me"},
                "guilds": [
                    {
                        "id": GUILD,
                        "properties": {"name": "Team"},
                        "channels": [{"id": GENERAL, "name": "general", "type": 0}],
                    }
                ],
                "private_channels": [{"id": DM, "type": 1, "recipient_ids": [TIAGO]}],
                "users": [{"id": TIAGO, "username": "tiago", "global_name": "Tiago"}],
            },
        }
    )


def _user(uid: str) -> dict[str, Any]:
    names = {SELF: ("me", "Me"), TIAGO: ("tiago", "Tiago")}
    username, display = names.get(uid, (f"u{uid}", None))
    return {"id": uid, "username": username, "global_name": display}


def _msg(
    mid: str,
    content: str = "hello",
    *,
    channel: str = GENERAL,
    guild: str | None = GUILD,
    author: str = TIAGO,
    attachments: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": mid,
        "channel_id": channel,
        "author": _user(author),
        "content": content,
        "timestamp": "2026-11-19T10:00:00.5+00:00",
        "edited_timestamp": None,
        "type": 0,
        "attachments": attachments or [],
        "embeds": [],
        "mentions": [],
        **extra,
    }
    if guild is not None:
        d["guild_id"] = guild
    return d


def _att(aid: str, url: str, name: str = "pic.png", size: int = 3) -> dict[str, Any]:
    return {"id": aid, "filename": name, "url": url, "size": size}


def _dispatch(a: MessageArchive, t: str, d: Any) -> None:
    a.handle_dispatch({"op": 0, "t": t, "d": d})


def _rows(root: Path, sql: str, *args: object) -> list[tuple[Any, ...]]:
    with sqlite3.connect(root / "archive.db") as db:
        return db.execute(sql, args).fetchall()


class TestStorage:
    def test_create_records_all_fields(self, archive: MessageArchive):
        _ready(archive)
        ref = _msg("1000", "original question", author=SELF)
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg(
                "1001",
                "answer",
                referenced_message=ref,
                message_reference={"message_id": "1000"},
                mentions=[_user(SELF)],
                embeds=[{"title": "Docs"}],
            ),
        )
        m = archive.get("1001")
        assert m is not None
        assert (m.channel_id, m.guild_id, m.guild_name) == (GENERAL, GUILD, "Team")
        assert m.channel_label == "#general"
        assert (m.author_id, m.author_username, m.author_display_name) == (
            TIAGO,
            "tiago",
            "Tiago",
        )
        assert m.timestamp == "2026-11-19T10:00:00.500000Z"
        assert m.content == "answer" and m.type == 0
        assert m.reply_to_id == "1000"
        assert m.reply_to_summary == "Me: original question"
        assert m.embeds == ({"title": "Docs"},)
        assert m.mentions[0]["id"] == SELF
        assert m.first_seen_at.endswith("Z") and not m.partial
        assert [v.content for v in m.versions] == ["answer"]
        raw = _rows(archive.root, "SELECT raw FROM messages WHERE id = '1001'")[0][0]
        assert json.loads(raw)["content"] == "answer"

    def test_timestamps_normalize_to_sortable_utc(self):
        assert normalize_timestamp("2026-11-19T12:00:00+02:00") == (
            "2026-11-19T10:00:00.000000Z"
        )
        assert normalize_timestamp("2026-11-19") == "2026-11-19T00:00:00.000000Z"

    def test_replayed_create_after_resume_is_not_duplicated(
        self, archive: MessageArchive, caplog: pytest.LogCaptureFixture
    ):
        _ready(archive)
        for _ in range(3):
            _dispatch(archive, "MESSAGE_CREATE", _msg("1001"))
        _dispatch(archive, "MESSAGE_UPDATE", _msg("1001", "edited"))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001"))
        assert _rows(archive.root, "SELECT COUNT(*) FROM messages") == [(1,)]
        assert _rows(archive.root, "SELECT COUNT(*) FROM message_versions") == [(2,)]
        assert archive.get("1001").content == "edited"  # type: ignore[union-attr]
        assert "Archive failed" not in caplog.text

    def test_own_messages_are_archived(self, archive: MessageArchive):
        _ready(archive)
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "mine", author=SELF))
        m = archive.get("1001")
        assert m is not None and m.author_id == SELF and m.content == "mine"

    def test_dm_is_labelled_by_the_other_participant(self, archive: MessageArchive):
        _ready(archive)
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg("1001", channel="501", guild=None, author=SELF),
        )
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg("1002", channel="501", guild=None, author="7"),
        )
        _dispatch(archive, "MESSAGE_CREATE", _msg("1003", channel=DM, guild=None))
        assert archive.get("1001").channel_label == "DM u7"  # type: ignore[union-attr]
        assert archive.get("1003").channel_label == "DM Tiago"  # type: ignore[union-attr]
        found = archive.search(SearchQuery(channel="tiago"))
        assert [m.id for m in found] == ["1003"]

    def test_names_from_guild_channel_and_thread_events(self, archive: MessageArchive):
        _dispatch(archive, "GUILD_CREATE", {"id": "301", "name": "Other"})
        _dispatch(archive, "GUILD_UPDATE", {"id": "301", "name": "Renamed"})
        _dispatch(
            archive,
            "CHANNEL_CREATE",
            {"id": "401", "guild_id": "301", "name": "ops", "type": 0},
        )
        _dispatch(archive, "CHANNEL_UPDATE", {"id": "401", "name": "ops-2"})
        _dispatch(
            archive,
            "THREAD_CREATE",
            {"id": "402", "guild_id": "301", "name": "bug", "parent_id": "401"},
        )
        _dispatch(
            archive,
            "THREAD_LIST_SYNC",
            {"guild_id": "301", "threads": [{"id": "403", "name": "synced"}]},
        )
        _dispatch(
            archive,
            "CHANNEL_CREATE",
            {"id": "502", "type": 3, "name": "trip", "recipients": [_user(TIAGO)]},
        )
        assert _rows(archive.root, "SELECT name FROM guilds") == [("Renamed",)]
        assert dict(_rows(archive.root, "SELECT id, guild_id FROM channels")) == {
            "401": "301",
            "402": "301",
            "403": "301",
            "502": None,
        }
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", channel="401", guild="301"))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1002", channel="502", guild=None))
        m = archive.get("1001")
        assert m is not None and (m.channel_label, m.guild_name) == (
            "#ops-2",
            "Renamed",
        )
        assert archive.get("1002").channel_label == "trip"  # type: ignore[union-attr]

    def test_files_are_private(self, archive: MessageArchive, root: Path):
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001"))
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "attachments").stat().st_mode) == 0o700
        for name in ("archive.db", "archive.db-wal"):
            assert stat.S_IMODE((root / name).stat().st_mode) == 0o600


class TestEdits:
    def test_every_version_is_kept(self, archive: MessageArchive):
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "v1"))
        for text, ts in (
            ("v2", "2026-11-19T10:01:00+00:00"),
            ("v3", "2026-11-19T10:02:00Z"),
        ):
            _dispatch(
                archive, "MESSAGE_UPDATE", _msg("1001", text, edited_timestamp=ts)
            )
        m = archive.get("1001")
        assert m is not None and m.content == "v3" and m.edited
        assert [(v.version, v.content) for v in m.versions] == [
            (1, "v1"),
            (2, "v2"),
            (3, "v3"),
        ]
        assert m.versions[2].edited_timestamp == "2026-11-19T10:02:00.000000Z"
        assert [x.id for x in archive.search(SearchQuery(text="v1"))] == ["1001"]
        assert [x.id for x in archive.search(SearchQuery(edited=True))] == ["1001"]

    def test_embed_unfurl_updates_embeds_without_a_new_version(
        self, archive: MessageArchive
    ):
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "see https://x.test"))
        embeds = [{"title": "X", "url": "https://x.test"}]
        _dispatch(
            archive,
            "MESSAGE_UPDATE",
            {"id": "1001", "channel_id": GENERAL, "guild_id": GUILD, "embeds": embeds},
        )
        _dispatch(
            archive, "MESSAGE_UPDATE", _msg("1001", "see https://x.test", embeds=embeds)
        )
        m = archive.get("1001")
        assert m is not None and m.embeds == tuple(embeds) and not m.edited
        assert len(m.versions) == 1 and m.versions[0].embeds == tuple(embeds)

    def test_full_edit_of_unseen_message_is_archived(self, archive: MessageArchive):
        _dispatch(archive, "MESSAGE_UPDATE", _msg("1001", "edited text"))
        m = archive.get("1001")
        assert m is not None and not m.partial and m.content == "edited text"
        assert len(m.versions) == 1

    def test_partial_edit_of_unseen_message_leaves_stub_filled_by_create(
        self, archive: MessageArchive
    ):
        _dispatch(
            archive,
            "MESSAGE_UPDATE",
            {"id": "1001", "channel_id": GENERAL, "embeds": [{"title": "E"}]},
        )
        stub = archive.get("1001")
        assert stub is not None and stub.partial and stub.versions == ()
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "full"))
        m = archive.get("1001")
        assert m is not None and not m.partial and m.content == "full"
        assert m.author_username == "tiago" and len(m.versions) == 1

    def test_replayed_older_edit_does_not_add_versions(self, archive: MessageArchive):
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "A"))
        b = _msg("1001", "B", edited_timestamp="2026-11-19T10:01:00Z")
        c = _msg("1001", "C", edited_timestamp="2026-11-19T10:02:00Z")
        for payload in (b, c, b, c):
            _dispatch(archive, "MESSAGE_UPDATE", payload)
        m = archive.get("1001")
        assert m is not None and m.content == "C"
        assert [v.content for v in m.versions] == ["A", "B", "C"]

    def test_partial_update_on_stub_is_kept_through_create(
        self, archive: MessageArchive
    ):
        _dispatch(archive, "MESSAGE_DELETE", {"id": "1001", "channel_id": GENERAL})
        _dispatch(
            archive,
            "MESSAGE_UPDATE",
            {
                "id": "1001",
                "channel_id": GENERAL,
                "content": "edited",
                "edited_timestamp": "2026-11-19T10:05:00Z",
            },
        )
        stub = archive.get("1001")
        assert stub is not None and stub.partial and stub.content == "edited"
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "original"))
        m = archive.get("1001")
        assert m is not None and not m.partial and m.deleted_at is not None
        assert m.content == "edited"
        assert [v.content for v in m.versions] == ["original", "edited"]

    def test_attachment_removed_by_edit_is_flagged_not_current(
        self, archive: MessageArchive, server: _Server
    ):
        url = server.serve("/rm.png", (200, b"rm"))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        assert archive.wait_idle(5)
        _dispatch(archive, "MESSAGE_UPDATE", _msg("1001", attachments=[]))
        (att,) = archive.get("1001").attachments  # type: ignore[union-attr]
        assert att.current is False and att.local_path is not None
        assert att.local_path.read_bytes() == b"rm"


class TestDeletes:
    def test_delete_keeps_content_and_is_idempotent(self, archive: MessageArchive):
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "secret"))
        d = {"id": "1001", "channel_id": GENERAL, "guild_id": GUILD}
        _dispatch(archive, "MESSAGE_DELETE", d)
        first = archive.get("1001")
        _dispatch(archive, "MESSAGE_DELETE", d)
        again = archive.get("1001")
        assert first is not None and again is not None
        assert again.content == "secret" and again.deleted_at == first.deleted_at
        assert [m.id for m in archive.search(SearchQuery(deleted=True))] == ["1001"]

    def test_bulk_delete(self, archive: MessageArchive):
        for mid in ("1001", "1002", "1003"):
            _dispatch(archive, "MESSAGE_CREATE", _msg(mid))
        _dispatch(
            archive,
            "MESSAGE_DELETE_BULK",
            {"ids": ["1001", "1003"], "channel_id": GENERAL, "guild_id": GUILD},
        )
        deleted = archive.search(SearchQuery(deleted=True))
        assert sorted(m.id for m in deleted) == ["1001", "1003"]

    def test_delete_of_unseen_message_is_recorded_as_stub(
        self, archive: MessageArchive
    ):
        _dispatch(
            archive,
            "MESSAGE_DELETE",
            {"id": "1239000000000000000", "channel_id": GENERAL},
        )
        m = archive.get("1239000000000000000")
        assert m is not None and m.partial and m.deleted_at is not None
        assert m.timestamp.startswith("2024-")  # derived from the snowflake


class TestAttachments:
    def test_download_success(self, archive: MessageArchive, server: _Server):
        url = server.serve("/a.png", (200, b"PNG"))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        assert archive.wait_idle(5)
        (att,) = archive.get("1001").attachments  # type: ignore[union-attr]
        assert att.status == "done" and att.error is None and att.size == 3
        assert att.content_type == "image/png"
        assert att.local_path == archive.root / "attachments" / GENERAL / "1001_pic.png"
        assert att.local_path.read_bytes() == b"PNG"
        assert stat.S_IMODE(att.local_path.stat().st_mode) == 0o600
        assert not list(att.local_path.parent.glob("*.part"))

    def test_404_marks_failed_without_retry(
        self, archive: MessageArchive, server: _Server
    ):
        url = server.serve("/gone.png", (404, b""))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        assert archive.wait_idle(5)
        (att,) = archive.get("1001").attachments  # type: ignore[union-attr]
        assert (att.status, att.error) == ("failed", "HTTP 404")
        assert server.hits["/gone.png"] == 1
        assert att.local_path is not None and not att.local_path.exists()

    def test_503_is_retried_then_succeeds(self, root: Path, server: _Server):
        sleeps: list[float] = []
        archive = MessageArchive(root, retry_sleep=sleeps.append)
        url = server.serve("/flaky.png", (503, b""), (429, b""), (200, b"OK!"))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        assert archive.wait_idle(5)
        (att,) = archive.get("1001").attachments  # type: ignore[union-attr]
        archive.close()
        assert att.status == "done" and server.hits["/flaky.png"] == 3
        assert sleeps == [1.0, 2.0]

    def test_persistent_5xx_gives_up_after_retries(
        self, archive: MessageArchive, server: _Server
    ):
        url = server.serve("/down.png", (500, b""))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        assert archive.wait_idle(5)
        assert server.hits["/down.png"] == 4
        assert _rows(archive.root, "SELECT status, attempts FROM attachments") == [
            ("failed", 4)
        ]

    def test_same_filename_twice_in_one_message_does_not_clash(
        self, root: Path, server: _Server
    ):
        archive = MessageArchive(root, workers=2)
        a = server.serve("/1.png", (200, b"one"))
        b = server.serve("/2.png", (200, b"two"))
        release = threading.Event()
        server.gate = {"/1.png": release, "/2.png": release}
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg(
                "1001",
                attachments=[_att("8", a, "x/../shot.png"), _att("9", b, "shot.png")],
            ),
        )
        release.set()
        assert archive.wait_idle(5)
        atts = archive.get("1001").attachments  # type: ignore[union-attr]
        archive.close()
        paths = {a.id: a.local_path for a in atts}
        assert paths["8"] != paths["9"]
        assert {p.name for p in paths.values() if p} == {
            "1001_shot.png",
            "1001_shot-2.png",
        }
        assert {p.read_bytes() for p in paths.values() if p} == {b"one", b"two"}

    def test_names_differing_only_in_case_do_not_clash(
        self, archive: MessageArchive, server: _Server
    ):
        a = server.serve("/u.png", (200, b"upper"))
        b = server.serve("/l.png", (200, b"lower"))
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg(
                "1001", attachments=[_att("8", a, "Shot.PNG"), _att("9", b, "shot.png")]
            ),
        )
        assert archive.wait_idle(5)
        atts = {x.id: x for x in archive.get("1001").attachments}  # type: ignore[union-attr]
        assert atts["8"].local_path != atts["9"].local_path
        assert atts["8"].local_path.read_bytes() == b"upper"  # type: ignore[union-attr]
        assert atts["9"].local_path.read_bytes() == b"lower"  # type: ignore[union-attr]

    def test_attachment_added_by_edit_is_downloaded(
        self, archive: MessageArchive, server: _Server
    ):
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", "look"))
        url = server.serve("/late.png", (200, b"late"))
        _dispatch(
            archive,
            "MESSAGE_UPDATE",
            _msg("1001", "look", attachments=[_att("9", url)]),
        )
        assert archive.wait_idle(5)
        m = archive.get("1001")
        assert m is not None and m.attachments[0].status == "done"
        assert [v.attachment_ids for v in m.versions] == [(), ("9",)]

    def test_file_survives_delete(self, archive: MessageArchive, server: _Server):
        url = server.serve("/keep.png", (200, b"keep"))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        assert archive.wait_idle(5)
        _dispatch(archive, "MESSAGE_DELETE", {"id": "1001", "channel_id": GENERAL})
        _dispatch(archive, "MESSAGE_UPDATE", _msg("1001", attachments=[]))
        m = archive.get("1001")
        assert m is not None and m.deleted_at is not None
        assert m.attachments[0].local_path.read_bytes() == b"keep"  # type: ignore[union-attr]

    def test_retry_failed_requeues(self, archive: MessageArchive, server: _Server):
        url = server.serve("/later.png", (404, b""))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        assert archive.wait_idle(5)
        server.serve("/later.png", (200, b"now"))
        assert archive.retry_failed() == 1
        assert archive.wait_idle(5)
        assert archive.get("1001").attachments[0].status == "done"  # type: ignore[union-attr]
        assert archive.retry_failed() == 0

    def test_pending_downloads_resume_after_crash(self, root: Path, server: _Server):
        url = server.serve("/r.png", (200, b"resumed"))
        interrupt_download(root, _msg("1001", attachments=[_att("9", url)]))
        assert _rows(root, "SELECT status FROM attachments") == [("pending",)]

        read_only = MessageArchive(root, resume=False)
        read_only.close()
        assert server.hits == {}

        second = MessageArchive(root)
        assert second.wait_idle(5)
        assert second.get("1001").attachments[0].status == "done"  # type: ignore[union-attr]
        second.close()

    def test_fetcher_crash_is_contained(self, root: Path):
        def boom(url: str, out: BinaryIO, *, max_bytes: int) -> str | None:
            raise RuntimeError("kaput")

        archive = MessageArchive(root, fetcher=boom)
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg("1001", attachments=[_att("9", "https://cdn/x")]),
        )
        assert archive.wait_idle(5)
        att = archive.get("1001").attachments[0]  # type: ignore[union-attr]
        archive.close()
        assert att.status == "failed" and "kaput" in (att.error or "")
        assert not list(root.rglob("*.part"))

    def test_size_cap(self, root: Path, server: _Server):
        archive = MessageArchive(root, max_file_bytes=4)
        big = server.serve("/big.png", (200, b"0123456789"))
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg(
                "1001",
                attachments=[
                    _att("8", big, "lies.png", size=2),
                    _att("9", big, "declared.png", size=10),
                ],
            ),
        )
        assert archive.wait_idle(5)
        atts = {a.id: a for a in archive.get("1001").attachments}  # type: ignore[union-attr]
        archive.close()
        assert atts["8"].status == "failed" and "too large" in (atts["8"].error or "")
        assert atts["9"].status == "failed" and "too large" in (atts["9"].error or "")
        assert server.hits["/big.png"] == 1
        assert not list(root.rglob("*.png*"))

    def test_filenames_are_sanitized(self, archive: MessageArchive, server: _Server):
        url = server.serve("/s", (200, b"x"))
        name = "../../etc/" + "é" * 200 + '<>:"|?*.txt'
        _dispatch(
            archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url, name)])
        )
        assert archive.wait_idle(5)
        path = archive.get("1001").attachments[0].local_path  # type: ignore[union-attr]
        assert (
            path is not None and path.parent == archive.root / "attachments" / GENERAL
        )
        assert len(path.name.encode()) <= 200 and path.name.endswith(".txt")
        assert path.read_bytes() == b"x"


def interrupt_download(root: Path, message: dict[str, Any]) -> None:
    """Close the archive while a download sits in retry backoff."""
    called = threading.Event()

    def flaky(url: str, out: BinaryIO, *, max_bytes: int) -> str | None:
        called.set()
        raise FetchError("HTTP 503", retryable=True)

    archive = MessageArchive(root, fetcher=flaky)
    _dispatch(archive, "MESSAGE_CREATE", message)
    assert called.wait(5)
    while _rows(root, "SELECT attempts FROM attachments") != [(1,)]:
        time.sleep(0.01)
    time.sleep(0.2)  # let the worker enter its 1s backoff
    started = time.monotonic()
    archive.close()
    assert time.monotonic() - started < 0.5  # did not sit out the 1s backoff


class TestRobustness:
    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"t": "MESSAGE_CREATE"},
            {"t": "MESSAGE_CREATE", "d": None},
            {"t": "MESSAGE_CREATE", "d": []},
            {"t": "MESSAGE_CREATE", "d": {"id": "abc", "channel_id": "1"}},
            {"t": "MESSAGE_CREATE", "d": {"id": "1", "channel_id": "2", "author": 5}},
            {
                "t": "MESSAGE_CREATE",
                "d": {"id": "1", "channel_id": "2", "attachments": [1, {"id": 3}]},
            },
            {"t": "MESSAGE_UPDATE", "d": {"id": "1"}},
            {"t": "MESSAGE_DELETE_BULK", "d": {"ids": "nope", "channel_id": "2"}},
            {"t": "READY", "d": {"guilds": "x", "private_channels": [None]}},
            {"t": "GUILD_CREATE", "d": {"id": 7}},
            {"t": "PRESENCE_UPDATE", "d": {"anything": True}},
            {"t": 5, "d": {}},
        ],
    )
    def test_garbage_payloads_never_raise(
        self, archive: MessageArchive, payload: dict[str, Any]
    ):
        archive.handle_dispatch(payload)

    def test_db_failure_is_logged_not_raised(self, archive: MessageArchive):
        with sqlite3.connect(archive.root / "archive.db") as db:
            db.execute("DROP TABLE message_versions")
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001"))
        assert _rows(archive.root, "SELECT COUNT(*) FROM messages") == [(0,)]
        _dispatch(archive, "MESSAGE_DELETE", {"id": "1002", "channel_id": GENERAL})
        assert _rows(archive.root, "SELECT COUNT(*) FROM messages") == [(1,)]

    def test_after_close_dispatch_is_ignored(self, root: Path):
        archive = MessageArchive(root)
        archive.close()
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001"))
        archive.close()


class TestSearch:
    def test_filters_and_order(self, archive: MessageArchive):
        _ready(archive)
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg("1001", "early", timestamp="2026-11-01T00:00:00Z"),
        )
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg("1002", "late 100%", timestamp="2026-11-18T00:00:00Z"),
        )
        _dispatch(
            archive,
            "MESSAGE_CREATE",
            _msg(
                "1003",
                "dm",
                channel=DM,
                guild=None,
                author=SELF,
                timestamp="2026-11-10T00:00:00Z",
            ),
        )

        def ids(**kw: Any) -> list[str]:
            return [m.id for m in archive.search(SearchQuery(**kw))]

        assert ids() == ["1001", "1003", "1002"]
        assert ids(limit=2) == ["1003", "1002"]
        assert ids(channel="gener") == ids(channel=GENERAL) == ["1001", "1002"]
        assert ids(guild="team") == ids(guild=GUILD) == ["1001", "1002"]
        assert ids(author="me") == ids(author=SELF) == ["1003"]
        assert ids(text="100%") == ["1002"]
        assert ids(text="%") == ["1002"]
        assert ids(since="2026-11-05T00:00:00.000000Z") == ["1003", "1002"]
        assert ids(until="2026-11-05T00:00:00.000000Z") == ["1001"]

    def test_stats(self, archive: MessageArchive, server: _Server):
        _ready(archive)
        url = server.serve("/a", (200, b"abcd"))
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001", attachments=[_att("9", url)]))
        _dispatch(archive, "MESSAGE_UPDATE", _msg("1001", "changed"))
        _dispatch(archive, "MESSAGE_DELETE", {"id": "1002", "channel_id": GENERAL})
        assert archive.wait_idle(5)
        s = archive.stats()
        assert (s.messages, s.deleted, s.edited, s.partial, s.versions) == (
            2,
            1,
            1,
            1,
            2,
        )
        assert s.attachments == {"done": 1} and s.attachment_bytes == 4
        assert (s.guilds, s.channels) == (1, 2)


def test_umask_does_not_widen_permissions(tmp_path: Path):
    old = os.umask(0)
    try:
        archive = MessageArchive(tmp_path / "a")
        _dispatch(archive, "MESSAGE_CREATE", _msg("1001"))
        archive.close()
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "a" / "archive.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "a").stat().st_mode) == 0o700
