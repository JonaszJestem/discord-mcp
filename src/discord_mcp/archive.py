"""Local, append-only archive of every gateway message the listener sees.

Fed with raw gateway dispatch payloads (not `MessageEvent`, which drops
attachment metadata, edits, deletes and own messages). Stores messages in
SQLite and downloads attachments in a small thread pool so the gateway
callback never waits on the network.
"""

from __future__ import annotations

import http.client
import json
import os
import pathlib as pl
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, Protocol, cast

from .logger import logger
from .models import Snowflake, snowflake, time_for_snowflake

JsonObj = dict[str, Any]

DB_NAME = "archive.db"
ATTACHMENTS_DIR = "attachments"
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_WORKERS = 3
_MAX_ATTEMPTS = 4  # first try + 3 retries
_BACKOFF_START_S = 1.0
_FETCH_TIMEOUT_S = 60.0
_CHUNK = 64 * 1024
_MAX_NAME_BYTES = 150
_MAX_SUFFIX_CHARS = 16
_SUMMARY_CHARS = 200
_DM_TYPES = frozenset({1, 3})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS guilds (
    id TEXT PRIMARY KEY, name TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, guild_id TEXT, name TEXT, type INTEGER, parent_id TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dm_participants (
    channel_id TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT,
    PRIMARY KEY (channel_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    guild_id TEXT,
    author_id TEXT,
    author_username TEXT,
    author_display_name TEXT,
    timestamp TEXT NOT NULL,
    edited_timestamp TEXT,
    content TEXT,
    type INTEGER,
    reply_to_id TEXT,
    reply_to_summary TEXT,
    embeds TEXT,
    mentions TEXT,
    raw TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    partial INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_channel_ts ON messages (channel_id, timestamp);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (timestamp);
CREATE TABLE IF NOT EXISTS message_versions (
    message_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT,
    attachments TEXT,
    embeds TEXT,
    edited_timestamp TEXT,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (message_id, version)
);
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    filename TEXT,
    url TEXT,
    size INTEGER,
    content_type TEXT,
    status TEXT NOT NULL,
    error TEXT,
    local_path TEXT UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    downloaded_at TEXT
);
CREATE INDEX IF NOT EXISTS attachments_message ON attachments (message_id);
CREATE INDEX IF NOT EXISTS attachments_status ON attachments (status);
"""


class FetchError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Fetcher(Protocol):
    def __call__(self, url: str, out: BinaryIO, *, max_bytes: int) -> str | None:
        """Write the body of `url` to `out`; return its content type."""
        ...


def urllib_fetch(url: str, out: BinaryIO, *, max_bytes: int) -> str | None:
    if not url.startswith(("https://", "http://")):
        raise FetchError(f"unsupported url scheme: {url[:40]!r}", retryable=False)
    request = urllib.request.Request(url, headers={"User-Agent": "discord-mcp"})
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
            resp = response
            length = resp.headers.get("Content-Length")
            if isinstance(length, str) and length.isdigit() and int(length) > max_bytes:
                raise FetchError(f"too large: {length} bytes", retryable=False)
            total = 0
            while chunk := cast(bytes, resp.read(_CHUNK)):
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(
                        f"too large: over {max_bytes} bytes", retryable=False
                    )
                out.write(chunk)
            content_type = resp.headers.get("Content-Type")
            return content_type if isinstance(content_type, str) else None
    except urllib.error.HTTPError as e:
        raise FetchError(
            f"HTTP {e.code}", retryable=e.code == 429 or e.code >= 500
        ) from e
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise FetchError(f"network error: {e}", retryable=True) from e


@dataclass(frozen=True, slots=True)
class ArchivedAttachment:
    id: str
    filename: str | None
    url: str | None
    size: int | None
    content_type: str | None
    status: str
    error: str | None
    local_path: pl.Path | None
    current: bool = True


@dataclass(frozen=True, slots=True)
class ArchivedVersion:
    version: int
    content: str | None
    attachment_ids: tuple[str, ...]
    embeds: tuple[JsonObj, ...]
    edited_timestamp: str | None
    seen_at: str


@dataclass(frozen=True, slots=True)
class ArchivedMessage:
    id: str
    channel_id: str
    channel_label: str
    guild_id: str | None
    guild_name: str | None
    author_id: str | None
    author_username: str | None
    author_display_name: str | None
    timestamp: str
    edited_timestamp: str | None
    content: str | None
    type: int | None
    reply_to_id: str | None
    reply_to_summary: str | None
    embeds: tuple[JsonObj, ...]
    mentions: tuple[JsonObj, ...]
    first_seen_at: str
    deleted_at: str | None
    partial: bool
    version_count: int
    attachments: tuple[ArchivedAttachment, ...] = ()
    versions: tuple[ArchivedVersion, ...] = ()

    @property
    def edited(self) -> bool:
        return self.version_count > 1 or self.edited_timestamp is not None


@dataclass(frozen=True, slots=True)
class ArchiveStats:
    messages: int
    deleted: int
    edited: int
    partial: int
    versions: int
    guilds: int
    channels: int
    attachments: dict[str, int]
    attachment_bytes: int
    oldest: str | None
    newest: str | None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    channel: str | None = None
    guild: str | None = None
    author: str | None = None
    text: str | None = None
    since: str | None = None
    until: str | None = None
    deleted: bool = False
    edited: bool = False
    limit: int = 50


class MessageArchive:
    def __init__(
        self,
        root: pl.Path,
        *,
        fetcher: Fetcher = urllib_fetch,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        workers: int = DEFAULT_WORKERS,
        retry_sleep: Callable[[float], None] | None = None,
        resume: bool = True,
    ) -> None:
        self.root = root
        self._fetcher = fetcher
        self._max_file_bytes = max_file_bytes
        self._stop = threading.Event()
        # Default backoff wakes early on close() so shutdown never waits it out.
        self._retry_sleep = retry_sleep or self._stop.wait
        self._lock = threading.RLock()
        self._closed = False
        self._futures: set[Future[None]] = set()
        _make_private_dir(root)
        _make_private_dir(root / ATTACHMENTS_DIR)
        db_path = root / DB_NAME
        if not db_path.exists():
            os.close(os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600))
        # SQLite creates -wal/-shm with the database file's permissions.
        self._db = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._db:
            self._db.executescript(_SCHEMA)
        self._self_id = self._meta("self_id")
        self._handlers: dict[str, Callable[[Any, list[str]], None]] = {
            "READY": self._on_ready,
            "GUILD_CREATE": self._on_guild,
            "GUILD_UPDATE": self._on_guild,
            "CHANNEL_CREATE": self._on_channel,
            "CHANNEL_UPDATE": self._on_channel,
            "THREAD_CREATE": self._on_channel,
            "THREAD_UPDATE": self._on_channel,
            "THREAD_LIST_SYNC": self._on_thread_list,
            "MESSAGE_CREATE": self._on_create,
            "MESSAGE_UPDATE": self._on_update,
            "MESSAGE_DELETE": self._on_delete,
            "MESSAGE_DELETE_BULK": self._on_delete_bulk,
        }
        self._executor = ThreadPoolExecutor(
            max_workers=max(workers, 1), thread_name_prefix="archive-dl"
        )
        if resume:
            self._submit_where("status = 'pending'")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
        # Queued and interrupted downloads stay `pending` and resume next start;
        # in-flight ones finish their current request and record the result.
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._db.close()

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                futures = set(self._futures)
            if not futures:
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            wait_futures(futures, timeout=remaining)

    def handle_dispatch(self, payload: JsonObj) -> None:
        """Ingest one gateway dispatch. Never raises."""
        kind = payload.get("t") if isinstance(payload, dict) else None  # pyright: ignore[reportUnnecessaryIsInstance]
        try:
            data = payload.get("d")
            handler = self._handlers.get(kind) if isinstance(kind, str) else None
            if handler is None or not isinstance(data, dict):
                return
            downloads: list[str] = []
            with self._lock:
                if self._closed:
                    return
                self_id = self._self_id
                try:
                    with self._db:
                        handler(data, downloads)
                except Exception:
                    self._self_id = self_id
                    raise
            self._submit(downloads)
        except Exception as e:
            logger.warning(f"Archive failed to store {kind!r}: {e!r}")

    def _on_ready(self, d: JsonObj, downloads: list[str]) -> None:
        self_id = _sid(_obj(d.get("user")).get("id"))
        if self_id is not None and self_id != self._self_id:
            self._self_id = self_id
            self._db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('self_id', ?)",
                (self_id,),
            )
        for guild in _objs(d.get("guilds")):
            self._on_guild(guild, downloads)
        users: dict[str, JsonObj] = {
            uid: user
            for user in _objs(d.get("users"))
            if (uid := _sid(user.get("id"))) is not None
        }
        for channel in _objs(d.get("private_channels")):
            self._on_channel(channel, downloads)
            cid = _sid(channel.get("id"))
            if cid is None:
                continue
            for rid in _list(channel.get("recipient_ids")):
                user = users.get(rid) if isinstance(rid, str) else None
                if user is not None:
                    self._remember_participant(cid, user)

    def _on_guild(self, d: JsonObj, _: list[str]) -> None:
        gid = _sid(d.get("id"))
        if gid is None:
            return
        name = _str(d.get("name")) or _str(_obj(d.get("properties")).get("name"))
        if name is not None:
            self._db.execute(
                "INSERT INTO guilds (id, name, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET name = excluded.name, "
                "updated_at = excluded.updated_at",
                (gid, name, _now()),
            )
        for channel in [*_objs(d.get("channels")), *_objs(d.get("threads"))]:
            self._upsert_channel(channel, gid)

    def _on_channel(self, d: JsonObj, _: list[str]) -> None:
        self._upsert_channel(d, _sid(d.get("guild_id")))
        cid = _sid(d.get("id"))
        if cid is not None:
            for user in _objs(d.get("recipients")):
                self._remember_participant(cid, user)

    def _on_thread_list(self, d: JsonObj, _: list[str]) -> None:
        gid = _sid(d.get("guild_id"))
        for thread in _objs(d.get("threads")):
            self._upsert_channel(thread, gid)

    def _upsert_channel(self, d: JsonObj, guild_id: str | None) -> None:
        cid = _sid(d.get("id"))
        if cid is None:
            return
        self._db.execute(
            "INSERT INTO channels (id, guild_id, name, type, parent_id, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "guild_id = COALESCE(excluded.guild_id, guild_id), "
            "name = COALESCE(excluded.name, name), "
            "type = COALESCE(excluded.type, type), "
            "parent_id = COALESCE(excluded.parent_id, parent_id), "
            "updated_at = excluded.updated_at",
            (
                cid,
                guild_id,
                _str(d.get("name")),
                _int(d.get("type")),
                _sid(d.get("parent_id")),
                _now(),
            ),
        )

    def _remember_participant(self, channel_id: str, user: JsonObj) -> None:
        uid = _sid(user.get("id"))
        if uid is None or uid == self._self_id:
            return
        self._db.execute(
            "INSERT INTO dm_participants (channel_id, user_id, name) VALUES (?, ?, ?) "
            "ON CONFLICT (channel_id, user_id) DO UPDATE SET "
            "name = COALESCE(excluded.name, name)",
            (channel_id, uid, _display_name(user)),
        )

    def _on_create(self, d: JsonObj, downloads: list[str]) -> None:
        mid, cid = _sid(d.get("id")), _sid(d.get("channel_id"))
        if mid is None or cid is None:
            return
        row = self._db.execute(
            "SELECT partial FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        if row is not None and not row[0]:
            return  # replayed after a gateway RESUME
        self._store_full(mid, cid, d, downloads, existing=row is not None)

    def _store_full(
        self,
        mid: str,
        cid: str,
        d: JsonObj,
        downloads: list[str],
        *,
        existing: bool,
    ) -> None:
        now = _now()
        stub = (
            self._db.execute(
                "SELECT content, edited_timestamp FROM messages WHERE id = ?", (mid,)
            ).fetchone()
            if existing
            else None
        )
        author = _obj(d.get("author"))
        gid = _sid(d.get("guild_id"))
        reply_id, reply_summary = _reply(d)
        values = (
            cid,
            gid,
            _sid(author.get("id")),
            _str(author.get("username")),
            _display_name(author),
            _timestamp(d.get("timestamp")) or _snowflake_time(mid),
            _timestamp(d.get("edited_timestamp")),
            _str(d.get("content")),
            _int(d.get("type")),
            reply_id,
            reply_summary,
            _dumps(_list(d.get("embeds"))),
            _dumps([_mention(m) for m in _objs(d.get("mentions"))]),
            _dumps(d),
            now,
        )
        if existing:
            # Fills a stub left by an unseen edit/delete; keeps deleted_at.
            self._db.execute(
                "UPDATE messages SET channel_id = ?, guild_id = ?, author_id = ?, "
                "author_username = ?, author_display_name = ?, timestamp = ?, "
                "edited_timestamp = ?, content = ?, type = ?, reply_to_id = ?, "
                "reply_to_summary = ?, embeds = ?, mentions = ?, raw = ?, "
                "updated_at = ?, partial = 0 WHERE id = ?",
                (*values, mid),
            )
        else:
            self._db.execute(
                "INSERT INTO messages (channel_id, guild_id, author_id, "
                "author_username, author_display_name, timestamp, edited_timestamp, "
                "content, type, reply_to_id, reply_to_summary, embeds, mentions, raw, "
                "updated_at, id, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, mid, now),
            )
        attachment_ids = self._store_attachments(mid, cid, d, downloads)
        if gid is None and self._self_id is not None:
            self._remember_participant(cid, author)
        has_version = self._db.execute(
            "SELECT 1 FROM message_versions WHERE message_id = ?", (mid,)
        ).fetchone()
        if has_version is None:
            self._add_version(mid, d, attachment_ids, content=_str(d.get("content")))
        stub_content = cast(str | None, stub[0]) if stub else None
        if stub_content is not None and stub_content != _str(d.get("content")):
            # A partial edit reached the stub before this (replayed) original.
            edit = {
                "embeds": d.get("embeds"),
                "edited_timestamp": stub[1] if stub else None,
            }
            self._add_version(mid, edit, attachment_ids, content=stub_content)
            self._db.execute(
                "UPDATE messages SET content = ?, "
                "edited_timestamp = COALESCE(?, edited_timestamp) WHERE id = ?",
                (stub_content, edit["edited_timestamp"], mid),
            )

    def _on_update(self, d: JsonObj, downloads: list[str]) -> None:
        mid, cid = _sid(d.get("id")), _sid(d.get("channel_id"))
        if mid is None or cid is None:
            return
        row = self._db.execute(
            "SELECT partial FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        full = isinstance(d.get("author"), dict) and "timestamp" in d and "content" in d
        if row is None or row[0]:
            if full:
                self._store_full(mid, cid, d, downloads, existing=row is not None)
            elif row is None:
                self._insert_stub(mid, cid, _sid(d.get("guild_id")), d, deleted=False)
            else:
                self._update_row(mid, d)
            return
        self._apply_edit(mid, cid, d, downloads)

    def _apply_edit(self, mid: str, cid: str, d: JsonObj, downloads: list[str]) -> None:
        stored_edit = self._db.execute(
            "SELECT edited_timestamp FROM messages WHERE id = ?", (mid,)
        ).fetchone()[0]
        incoming_edit = _timestamp(d.get("edited_timestamp"))
        if stored_edit and incoming_edit and incoming_edit < stored_edit:
            return  # an older edit replayed after a newer one
        latest = self._db.execute(
            "SELECT version, content, attachments FROM message_versions "
            "WHERE message_id = ? ORDER BY version DESC LIMIT 1",
            (mid,),
        ).fetchone()
        prev_content = cast(str | None, latest[1]) if latest else None
        prev_attachments = _loads_list(latest[2]) if latest else []
        content = _str(d.get("content")) if "content" in d else prev_content
        attachment_ids = (
            self._store_attachments(mid, cid, d, downloads)
            if "attachments" in d
            else prev_attachments
        )
        self._update_row(mid, d)
        if latest is None or (content, attachment_ids) != (
            prev_content,
            prev_attachments,
        ):
            self._add_version(mid, d, attachment_ids, content=content)
        elif "embeds" in d:
            # An unfurl adds embeds to the same text: not a new version.
            self._db.execute(
                "UPDATE message_versions SET embeds = ? "
                "WHERE message_id = ? AND version = ?",
                (_dumps(_list(d.get("embeds"))), mid, cast(int, latest[0])),
            )

    def _update_row(self, mid: str, d: JsonObj) -> None:
        """Apply the fields a (possibly partial) MESSAGE_UPDATE carries."""
        sets: dict[str, object] = {"updated_at": _now(), "raw": _dumps(d)}
        if "content" in d:
            sets["content"] = _str(d.get("content"))
        if "embeds" in d:
            sets["embeds"] = _dumps(_list(d.get("embeds")))
        if "edited_timestamp" in d:
            sets["edited_timestamp"] = _timestamp(d.get("edited_timestamp"))
        if "mentions" in d:
            sets["mentions"] = _dumps([_mention(m) for m in _objs(d.get("mentions"))])
        self._db.execute(
            f"UPDATE messages SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), mid),
        )

    def _add_version(
        self,
        mid: str,
        d: JsonObj,
        attachment_ids: list[str],
        *,
        content: str | None,
    ) -> None:
        self._db.execute(
            "INSERT INTO message_versions (message_id, version, content, attachments, "
            "embeds, edited_timestamp, seen_at) VALUES (?, "
            "(SELECT COALESCE(MAX(version), 0) + 1 FROM message_versions "
            "WHERE message_id = ?), ?, ?, ?, ?, ?)",
            (
                mid,
                mid,
                content,
                _dumps(attachment_ids),
                _dumps(_list(d.get("embeds"))),
                _timestamp(d.get("edited_timestamp")),
                _now(),
            ),
        )

    def _store_attachments(
        self, mid: str, cid: str, d: JsonObj, downloads: list[str]
    ) -> list[str]:
        ids: list[str] = []
        for att in _objs(d.get("attachments")):
            aid = _sid(att.get("id"))
            if aid is None:
                continue
            ids.append(aid)
            if self._db.execute(
                "SELECT 1 FROM attachments WHERE id = ?", (aid,)
            ).fetchone():
                continue
            name = _str(att.get("filename")) or aid
            size = _int(att.get("size"))
            url = _str(att.get("url")) or _str(att.get("proxy_url"))
            status, error = "pending", None
            if url is None:
                status, error = "failed", "no url in payload"
            elif size is not None and size > self._max_file_bytes:
                status, error = "failed", f"too large: {size} bytes"
            self._db.execute(
                "INSERT INTO attachments (id, message_id, channel_id, filename, url, "
                "size, content_type, status, error, local_path, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    aid,
                    mid,
                    cid,
                    name,
                    url,
                    size,
                    _str(att.get("content_type")),
                    status,
                    error,
                    self._reserve_path(cid, mid, name),
                    _now(),
                ),
            )
            if status == "pending":
                downloads.append(aid)
        return ids

    def _reserve_path(self, cid: str, mid: str, filename: str) -> str:
        """Pick a path no other attachment owns; caller holds the lock."""
        stem, suffix = _split_name(_sanitize_filename(filename))
        n = 1
        while True:
            name = f"{mid}_{stem}{suffix}" if n == 1 else f"{mid}_{stem}-{n}{suffix}"
            rel = f"{ATTACHMENTS_DIR}/{cid}/{name}"
            taken = self._db.execute(
                # COLLATE NOCASE: macOS volumes are case-insensitive by default.
                "SELECT 1 FROM attachments WHERE local_path = ? COLLATE NOCASE",
                (rel,),
            ).fetchone()
            if taken is None and not (self.root / rel).exists():
                return rel
            n += 1

    def _on_delete(self, d: JsonObj, _: list[str]) -> None:
        self._mark_deleted([d.get("id")], d)

    def _on_delete_bulk(self, d: JsonObj, _: list[str]) -> None:
        self._mark_deleted(_list(d.get("ids")), d)

    def _mark_deleted(self, raw_ids: Iterable[object], d: JsonObj) -> None:
        cid = _sid(d.get("channel_id"))
        gid = _sid(d.get("guild_id"))
        now = _now()
        for raw in raw_ids:
            mid = _sid(raw)
            if mid is None:
                continue
            cur = self._db.execute(
                "UPDATE messages SET deleted_at = COALESCE(deleted_at, ?), "
                "updated_at = ? WHERE id = ?",
                (now, now, mid),
            )
            if cur.rowcount == 0 and cid is not None:
                self._insert_stub(mid, cid, gid, d, deleted=True)

    def _insert_stub(
        self, mid: str, cid: str, gid: str | None, d: JsonObj, *, deleted: bool
    ) -> None:
        now = _now()
        self._db.execute(
            "INSERT INTO messages (id, channel_id, guild_id, timestamp, content, "
            "embeds, raw, first_seen_at, updated_at, deleted_at, partial) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                mid,
                cid,
                gid,
                _snowflake_time(mid),
                _str(d.get("content")),
                _dumps(_list(d.get("embeds"))),
                _dumps(d),
                now,
                now,
                now if deleted else None,
            ),
        )

    def retry_failed(self) -> int:
        with self._lock:
            with self._db:
                ids = [
                    cast(str, r[0])
                    for r in self._db.execute(
                        "SELECT id FROM attachments WHERE status = 'failed' "
                        "AND url IS NOT NULL"
                    ).fetchall()
                ]
                self._db.executemany(
                    "UPDATE attachments SET status = 'pending', error = NULL, "
                    "attempts = 0 WHERE id = ?",
                    [(i,) for i in ids],
                )
        self._submit(ids)
        return len(ids)

    def _submit_where(self, where: str) -> None:
        with self._lock:
            ids = [
                cast(str, r[0])
                for r in self._db.execute(f"SELECT id FROM attachments WHERE {where}")
            ]
        self._submit(ids)

    def _submit(self, attachment_ids: list[str]) -> None:
        for aid in attachment_ids:
            with self._lock:
                if self._closed:
                    return
                future = self._executor.submit(self._download, aid)
                self._futures.add(future)
            future.add_done_callback(self._forget)

    def _forget(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)

    def _download(self, aid: str) -> None:
        try:
            self._download_once(aid)
        except Exception as e:
            logger.warning(f"Archive download of attachment {aid} crashed: {e!r}")
            self._finish(aid, "failed", error=f"crashed: {e!r}")

    def _download_once(self, aid: str) -> None:
        with self._lock:
            if self._closed:
                return
            row = self._db.execute(
                "SELECT url, local_path, status FROM attachments WHERE id = ?", (aid,)
            ).fetchone()
        if row is None or row[2] != "pending":
            return
        url, rel = cast(str, row[0]), cast(str, row[1])
        dest = self.root / rel
        if dest.exists():
            # Renamed into place before a crash, DB not updated yet.
            self._finish(aid, "done", size=dest.stat().st_size)
            return
        _make_private_dir(dest.parent)
        backoff = _BACKOFF_START_S
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            part = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.part")
            try:
                with os.fdopen(
                    os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
                ) as out:
                    content_type = self._fetcher(
                        url, out, max_bytes=self._max_file_bytes
                    )
                os.replace(part, dest)
            except FetchError as e:
                part.unlink(missing_ok=True)
                self._record_attempt(aid, attempt, str(e))
                if self._stop.is_set():
                    return
                if not e.retryable or attempt == _MAX_ATTEMPTS:
                    self._finish(aid, "failed", error=str(e))
                    return
                self._retry_sleep(backoff)
                if self._stop.is_set():
                    return
                backoff *= 2
                continue
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            self._finish(
                aid, "done", size=dest.stat().st_size, content_type=content_type
            )
            return

    def _record_attempt(self, aid: str, attempt: int, error: str) -> None:
        with self._lock:
            with self._db:
                self._db.execute(
                    "UPDATE attachments SET attempts = ?, error = ? WHERE id = ?",
                    (attempt, error, aid),
                )

    def _finish(
        self,
        aid: str,
        status: str,
        *,
        error: str | None = None,
        size: int | None = None,
        content_type: str | None = None,
    ) -> None:
        with self._lock:
            with self._db:
                self._db.execute(
                    "UPDATE attachments SET status = ?, error = ?, "
                    "size = COALESCE(?, size), "
                    "content_type = COALESCE(content_type, ?), "
                    "downloaded_at = CASE WHEN ? = 'done' THEN ? END WHERE id = ?",
                    (status, error, size, content_type, status, _now(), aid),
                )

    def search(self, query: SearchQuery) -> list[ArchivedMessage]:
        """Newest `limit` matches, returned oldest first."""
        where: list[str] = []
        args: list[object] = []
        if query.channel:
            needle = query.channel.lstrip("#")
            if needle.isdigit():
                where.append("m.channel_id = ?")
                args.append(needle)
            else:
                where.append(
                    "(c.name LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM "
                    "dm_participants p WHERE p.channel_id = m.channel_id "
                    "AND p.name LIKE ? ESCAPE '\\'))"
                )
                args += [_like(needle)] * 2
        if query.guild:
            if query.guild.isdigit():
                where.append("m.guild_id = ?")
                args.append(query.guild)
            else:
                where.append("g.name LIKE ? ESCAPE '\\'")
                args.append(_like(query.guild))
        if query.author:
            if query.author.isdigit():
                where.append("m.author_id = ?")
                args.append(query.author)
            else:
                where.append(
                    "(m.author_username LIKE ? ESCAPE '\\' "
                    "OR m.author_display_name LIKE ? ESCAPE '\\')"
                )
                args += [_like(query.author)] * 2
        if query.text:
            where.append(
                "(m.content LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM "
                "message_versions v WHERE v.message_id = m.id "
                "AND v.content LIKE ? ESCAPE '\\'))"
            )
            args += [_like(query.text)] * 2
        if query.since:
            where.append("m.timestamp >= ?")
            args.append(query.since)
        if query.until:
            where.append("m.timestamp <= ?")
            args.append(query.until)
        if query.deleted:
            where.append("m.deleted_at IS NOT NULL")
        if query.edited:
            where.append(
                "(m.edited_timestamp IS NOT NULL OR (SELECT COUNT(*) FROM "
                "message_versions v WHERE v.message_id = m.id) > 1)"
            )
        sql = (
            f"{_SELECT_MESSAGE} {'WHERE ' + ' AND '.join(where) if where else ''} "
            "ORDER BY m.timestamp DESC, m.id DESC LIMIT ?"
        )
        args.append(max(query.limit, 1))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            found = [self._hydrate(r, versions=False) for r in rows]
        found.reverse()
        return found

    def get(self, message_id: str) -> ArchivedMessage | None:
        with self._lock:
            row = self._db.execute(
                f"{_SELECT_MESSAGE} WHERE m.id = ?", (message_id,)
            ).fetchone()
            return None if row is None else self._hydrate(row, versions=True)

    def stats(self) -> ArchiveStats:
        with self._lock:
            db = self._db

            def one(sql: str) -> Any:
                return db.execute(sql).fetchone()[0]

            statuses = {
                cast(str, s): cast(int, n)
                for s, n in db.execute(
                    "SELECT status, COUNT(*) FROM attachments GROUP BY status"
                )
            }
            return ArchiveStats(
                messages=one("SELECT COUNT(*) FROM messages"),
                deleted=one(
                    "SELECT COUNT(*) FROM messages WHERE deleted_at IS NOT NULL"
                ),
                edited=one(
                    "SELECT COUNT(DISTINCT message_id) FROM message_versions "
                    "WHERE version > 1"
                ),
                partial=one("SELECT COUNT(*) FROM messages WHERE partial = 1"),
                versions=one("SELECT COUNT(*) FROM message_versions"),
                guilds=one("SELECT COUNT(*) FROM guilds"),
                channels=one("SELECT COUNT(*) FROM channels"),
                attachments=statuses,
                attachment_bytes=one(
                    "SELECT COALESCE(SUM(size), 0) FROM attachments "
                    "WHERE status = 'done'"
                ),
                oldest=one("SELECT MIN(timestamp) FROM messages"),
                newest=one("SELECT MAX(timestamp) FROM messages"),
            )

    def _hydrate(
        self, row: sqlite3.Row | tuple[Any, ...], *, versions: bool
    ) -> ArchivedMessage:
        r = cast(tuple[Any, ...], row)
        mid = cast(str, r[0])
        latest = self._db.execute(
            "SELECT attachments FROM message_versions WHERE message_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (mid,),
        ).fetchone()
        current_ids = set(_loads_list(latest[0])) if latest else None
        attachments = tuple(
            ArchivedAttachment(
                id=a[0],
                filename=a[1],
                url=a[2],
                size=a[3],
                content_type=a[4],
                status=a[5],
                error=a[6],
                local_path=self.root / a[7] if a[7] else None,
                current=current_ids is None or a[0] in current_ids,
            )
            for a in self._db.execute(
                "SELECT id, filename, url, size, content_type, status, error, "
                "local_path FROM attachments WHERE message_id = ? "
                "ORDER BY first_seen_at, id",
                (mid,),
            ).fetchall()
        )
        history: tuple[ArchivedVersion, ...] = ()
        if versions:
            history = tuple(
                ArchivedVersion(
                    version=v[0],
                    content=v[1],
                    attachment_ids=tuple(_loads_list(v[2])),
                    embeds=tuple(_loads_objs(v[3])),
                    edited_timestamp=v[4],
                    seen_at=v[5],
                )
                for v in self._db.execute(
                    "SELECT version, content, attachments, embeds, edited_timestamp, "
                    "seen_at FROM message_versions WHERE message_id = ? "
                    "ORDER BY version",
                    (mid,),
                ).fetchall()
            )
        return ArchivedMessage(
            id=mid,
            channel_id=r[1],
            channel_label=_channel_label(r[1], r[2], r[3], r[19]),
            guild_id=r[4],
            guild_name=r[5],
            author_id=r[6],
            author_username=r[7],
            author_display_name=r[8],
            timestamp=r[9],
            edited_timestamp=r[10],
            content=r[11],
            type=r[12],
            reply_to_id=r[13],
            reply_to_summary=r[14],
            embeds=tuple(_loads_objs(r[15])),
            mentions=tuple(_loads_objs(r[16])),
            first_seen_at=r[17],
            deleted_at=r[18],
            partial=bool(r[20]),
            version_count=r[21],
            attachments=attachments,
            versions=history,
        )

    def _meta(self, key: str) -> str | None:
        row = self._db.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else cast(str, row[0])


_SELECT_MESSAGE = """
SELECT m.id, m.channel_id, c.name, c.type, m.guild_id, g.name, m.author_id,
       m.author_username, m.author_display_name, m.timestamp, m.edited_timestamp,
       m.content, m.type, m.reply_to_id, m.reply_to_summary, m.embeds, m.mentions,
       m.first_seen_at, m.deleted_at,
       (SELECT GROUP_CONCAT(name, ', ') FROM (SELECT p.name FROM dm_participants p
        WHERE p.channel_id = m.channel_id AND p.name IS NOT NULL ORDER BY p.name)),
       m.partial,
       (SELECT COUNT(*) FROM message_versions v WHERE v.message_id = m.id)
FROM messages m
LEFT JOIN channels c ON c.id = m.channel_id
LEFT JOIN guilds g ON g.id = m.guild_id
"""


def _channel_label(
    channel_id: str, name: str | None, ctype: int | None, participants: str | None
) -> str:
    if name and ctype not in _DM_TYPES:
        return f"#{name}"
    if name:
        return name
    if participants:
        return f"DM {participants}"
    return channel_id


def _make_private_dir(path: pl.Path) -> None:
    if path.is_dir():
        return
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _now() -> str:
    return _format_ts(datetime.now(UTC))


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def normalize_timestamp(value: str) -> str:
    """ISO-8601 → fixed-width UTC (`YYYY-MM-DDTHH:MM:SS.ffffffZ`), so strings sort by time.

    Naive values are taken as UTC. Raises ValueError for anything else.
    """
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return _format_ts(dt)


def _timestamp(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    try:
        return normalize_timestamp(raw)
    except ValueError:
        return None


def _snowflake_time(mid: str) -> str:
    return _format_ts(time_for_snowflake(mid))


def _sid(raw: object) -> Snowflake | None:
    if isinstance(raw, str) and raw.isascii() and raw.isdigit():
        return snowflake(raw)
    return None


def _str(raw: object) -> str | None:
    return raw if isinstance(raw, str) else None


def _int(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else None


def _obj(raw: object) -> JsonObj:
    return cast(JsonObj, raw) if isinstance(raw, dict) else {}


def _list(raw: object) -> list[Any]:
    return cast(list[Any], raw) if isinstance(raw, list) else []


def _objs(raw: object) -> list[JsonObj]:
    return [cast(JsonObj, x) for x in _list(raw) if isinstance(x, dict)]


def _display_name(user: JsonObj) -> str | None:
    return (
        _str(user.get("global_name"))
        or _str(user.get("display_name"))
        or _str(user.get("username"))
    )


def _mention(user: JsonObj) -> JsonObj:
    return {
        "id": _str(user.get("id")),
        "username": _str(user.get("username")),
        "display_name": _display_name(user),
    }


def _reply(d: JsonObj) -> tuple[str | None, str | None]:
    ref = _obj(d.get("referenced_message"))
    reply_id = _sid(ref.get("id")) or _sid(
        _obj(d.get("message_reference")).get("message_id")
    )
    if not ref:
        return reply_id, None
    who = _display_name(_obj(ref.get("author"))) or "unknown"
    text = " ".join((_str(ref.get("content")) or "").split())
    if len(text) > _SUMMARY_CHARS:
        text = text[: _SUMMARY_CHARS - 1] + "…"
    files = len(_list(ref.get("attachments")))
    if files:
        text = f"{text} [{files} file{'s' if files > 1 else ''}]".strip()
    return reply_id, f"{who}: {text}"


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads_list(raw: object) -> list[str]:
    if not isinstance(raw, str):
        return []
    value = json.loads(raw)
    return (
        [x for x in cast(list[Any], value) if isinstance(x, str)]
        if isinstance(value, list)
        else []
    )


def _loads_objs(raw: object) -> list[JsonObj]:
    if not isinstance(raw, str):
        return []
    return _objs(json.loads(raw))


def _like(needle: str) -> str:
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def _sanitize_filename(name: str) -> str:
    base = re.split(r"[/\\]", unicodedata.normalize("NFC", name))[-1]
    base = _UNSAFE_CHARS.sub("_", base).strip(" .")
    stem, suffix = _split_name(base or "file")
    if len(suffix) > _MAX_SUFFIX_CHARS:
        stem, suffix = stem + suffix, ""
    budget = _MAX_NAME_BYTES - len(suffix.encode())
    stem = stem.encode()[:budget].decode(errors="ignore") or "file"
    return stem + suffix


def _split_name(name: str) -> tuple[str, str]:
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]
