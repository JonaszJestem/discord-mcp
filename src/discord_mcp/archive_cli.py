"""`discord-mcp archive` — read and maintain the local message archive."""

from __future__ import annotations

import argparse
import json
import pathlib as pl
import re
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

from .archive import (
    DB_NAME,
    ArchivedAttachment,
    ArchivedMessage,
    MessageArchive,
    SearchQuery,
    normalize_timestamp,
)

_RELATIVE_RE = re.compile(r"(\d+)\s*([smhdw])")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_UNITS = {"s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}


def run_archive_cli(
    argv: list[str], root: pl.Path, *, out: TextIO | None = None
) -> int:
    out = out or sys.stdout
    ns = _parser().parse_args(argv)
    try:
        since = parse_time(ns.since) if getattr(ns, "since", None) else None
        until = (
            parse_time(ns.until, end_of_day=True)
            if getattr(ns, "until", None)
            else None
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not (root / DB_NAME).is_file():
        print(
            f"error: no archive at {root}; run `discord-mcp watch --archive` first",
            file=sys.stderr,
        )
        return 1

    # Only `retry` may download; the read commands must not requeue anything.
    archive = MessageArchive(root, resume=ns.command == "retry")
    try:
        if ns.command == "search":
            query = SearchQuery(
                channel=ns.channel,
                guild=ns.guild,
                author=ns.author,
                text=ns.text,
                since=since,
                until=until,
                deleted=ns.deleted,
                edited=ns.edited,
                limit=ns.limit,
            )
            found = archive.search(query)
            if ns.json:
                for message in found:
                    print(json.dumps(_to_json(message), ensure_ascii=False), file=out)
            else:
                for message in found:
                    print(_printable(format_message(message)), file=out)
            return 0
        if ns.command == "show":
            message = archive.get(ns.message_id)
            if message is None:
                print(f"error: message {ns.message_id} not archived", file=sys.stderr)
                return 1
            print(_printable(format_history(message)), file=out)
            return 0
        if ns.command == "stats":
            print(json.dumps(asdict(archive.stats()), indent=2), file=out)
            return 0
        queued = archive.retry_failed()
        archive.wait_idle()
        stats = archive.stats()
        print(f"retried {queued} failed download(s); now {stats.attachments}", file=out)
        if stats.attachments.get("failed"):
            print(
                "note: Discord CDN links expire after about a day; "
                "older failures usually cannot be recovered",
                file=out,
            )
        return 0
    finally:
        archive.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discord-mcp archive")
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="find archived messages")
    search.add_argument("--channel", help="channel id, or part of its name / DM name")
    search.add_argument("--guild", help="server id, or part of its name")
    search.add_argument("--author", help="author id, or part of their name")
    search.add_argument("--text", help="substring of the content, any edit version")
    search.add_argument("--since", help="24h, 7d, 30m or an ISO date/time (UTC)")
    search.add_argument("--until", help="same formats as --since")
    search.add_argument("--deleted", action="store_true", help="only deleted messages")
    search.add_argument("--edited", action="store_true", help="only edited messages")
    search.add_argument("--limit", type=int, default=50, help="newest N (default 50)")
    search.add_argument("--json", action="store_true", help="one JSON object per line")

    show = commands.add_parser("show", help="one message with its edit history")
    show.add_argument("message_id")

    commands.add_parser("stats", help="archive counts")
    commands.add_parser("retry", help="download failed attachments again")
    return parser


def parse_time(
    raw: str, *, now: datetime | None = None, end_of_day: bool = False
) -> str:
    """`24h` / `7d` (ago) or ISO-8601 → the archive's sortable UTC string.

    `end_of_day` makes a bare date inclusive (`--until 2026-11-19`).
    """
    text = raw.strip()
    if end_of_day and _DATE_RE.fullmatch(text):
        text += "T23:59:59.999999"
    relative = _RELATIVE_RE.fullmatch(text)
    if relative:
        seconds = int(relative.group(1)) * _UNITS[relative.group(2)]
        moment = (now or datetime.now(UTC)) - timedelta(seconds=seconds)
        return normalize_timestamp(moment.isoformat())
    try:
        return normalize_timestamp(text)
    except ValueError:
        raise ValueError(
            f"bad time {raw!r}: use e.g. 30m, 24h, 7d, 2w or 2026-11-19T10:00"
        ) from None


def format_message(m: ArchivedMessage) -> str:
    where = m.channel_label + (f" ({m.guild_name})" if m.guild_name else "")
    author = m.author_display_name or m.author_username or m.author_id or "?"
    flags = "".join(
        flag
        for flag, on in (
            (" (edited)", m.edited),
            (" (DELETED)", m.deleted_at is not None),
            (" (partial)", m.partial),
        )
        if on
    )
    lines = [f"{_short_ts(m.timestamp)}  {where}  {author}{flags}  [{m.id}]"]
    if m.reply_to_id:
        lines.append(f"    > reply to {m.reply_to_summary or m.reply_to_id}")
    if m.content:
        lines.extend(f"    {line}" for line in m.content.splitlines())
    for embed in m.embeds:
        title = embed.get("title") or embed.get("url")
        if isinstance(title, str):
            lines.append(f"    [embed] {title}")
    lines.extend(f"    {_attachment_line(a)}" for a in m.attachments)
    return "\n".join(lines)


def format_history(m: ArchivedMessage) -> str:
    lines = [format_message(m), ""]
    lines.append(f"first seen {_short_ts(m.first_seen_at)}")
    if m.deleted_at:
        lines.append(f"deleted    {_short_ts(m.deleted_at)}")
    by_id = {a.id: a for a in m.attachments}
    for v in m.versions:
        stamp = _short_ts(v.edited_timestamp or v.seen_at)
        lines.append(f"\n--- version {v.version} ({stamp})")
        lines.extend(f"    {line}" for line in (v.content or "").splitlines())
        for aid in v.attachment_ids:
            att = by_id.get(aid)
            lines.append(f"    {_attachment_line(att) if att else f'[file] {aid}'}")
    return "\n".join(lines)


def _attachment_line(a: ArchivedAttachment) -> str:
    tag = "file" if a.current else "file removed by edit"
    if a.status == "done" and a.local_path is not None:
        return f"[{tag}] {a.local_path}"
    detail = f": {a.error}" if a.error else ""
    return f"[{tag}, {a.status}{detail}] {a.filename or a.id}"


def _printable(text: str) -> str:
    """Drop control characters so message text cannot drive the terminal."""
    return _CONTROL_RE.sub("", text)


def _short_ts(ts: str) -> str:
    return ts[:19].replace("T", " ") + "Z"


def _to_json(m: ArchivedMessage) -> dict[str, Any]:
    data = asdict(m)
    data["edited"] = m.edited
    data["attachments"] = [
        {**asdict(a), "local_path": str(a.local_path) if a.local_path else None}
        for a in m.attachments
    ]
    data.pop("versions")
    return data
