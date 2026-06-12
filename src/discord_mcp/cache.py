"""Local discovery cache: a JSON file of servers/channels/people we've seen.

Populated opportunistically from data the tools already fetch (no extra API
calls). Its main job is handle -> snowflake resolution so `get_user_messages`
can take a username like `bobtheshoplifter` instead of a raw ID.

The cache is best-effort: a missing or corrupt file is treated as empty rather
than fatal, and writes are atomic-per-call (a single synchronous dump with no
await points, so concurrent coroutines can't interleave a half-written file).
"""

from __future__ import annotations

import json
import pathlib as pl
from typing import Any, Iterable, cast

from .models import Channel, Guild, Message


class DiscoveryCache:
    """A persisted record of discovered servers, channels, and people."""

    def __init__(self, path: pl.Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, Any]] = self._load()

    # ------------------------------------------------------------ record

    def record_guilds(self, guilds: Iterable[Guild]) -> None:
        servers = self._data["servers"]
        changed = False
        for g in guilds:
            entry = {"name": g.name}
            if servers.get(g.id) != entry:
                servers[g.id] = entry
                changed = True
        if changed:
            self._save()

    def record_channels(self, channels: Iterable[Channel]) -> None:
        known = self._data["channels"]
        changed = False
        for c in channels:
            entry = {"name": c.name, "guild_id": c.guild_id, "type": c.type}
            if known.get(c.id) != entry:
                known[c.id] = entry
                changed = True
        if changed:
            self._save()

    def record_people(self, messages: Iterable[Message]) -> None:
        """Index any message author that carries a snowflake."""
        people = self._data["people"]
        changed = False
        for m in messages:
            if not m.author_id:
                continue
            existing = people.get(m.author_id, {})
            seen = existing.get("last_seen")
            newer = seen is None or m.timestamp.isoformat() > seen
            entry = {
                "name": m.author_name,
                "last_seen": m.timestamp.isoformat() if newer else seen,
            }
            if existing != entry:
                people[m.author_id] = entry
                changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------ query

    def resolve_person(self, name: str) -> str | None:
        """Map a username (case-insensitive) to a cached snowflake, if known.

        A numeric input is returned as-is — callers may pass either form.
        """
        if name.isdigit():
            return name
        needle = name.lstrip("@").casefold()
        for person_id, info in self._data["people"].items():
            if str(info.get("name", "")).casefold() == needle:
                return person_id
        return None

    def known_people(self) -> list[dict[str, str]]:
        return [
            {"id": pid, "name": info.get("name", ""), "last_seen": info.get("last_seen", "")}
            for pid, info in self._data["people"].items()
        ]

    # ------------------------------------------------------------ io

    def _load(self) -> dict[str, dict[str, Any]]:
        empty: dict[str, dict[str, Any]] = {
            "servers": {},
            "channels": {},
            "people": {},
        }
        try:
            parsed: Any = json.loads(self._path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return empty
        if not isinstance(parsed, dict):
            return empty
        raw = cast(dict[str, Any], parsed)
        for key in empty:
            section = raw.get(key)
            if isinstance(section, dict):
                empty[key] = cast(dict[str, Any], section)
        return empty

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        except OSError:
            # A cache that can't persist still works in-memory for this run.
            pass
