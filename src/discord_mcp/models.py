"""Domain models — pure data, no I/O.

Snowflake is a NewType with a smart constructor. The validator runs exactly
once, at the edge of the domain, so internal code can trust that anything
typed as Snowflake is a numeric Discord ID.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NewType

from .errors import InvalidSnowflake


Snowflake = NewType("Snowflake", str)
SessionData = dict[str, Any]

# Discord's epoch (2015-01-01T00:00:00Z) in milliseconds. Every snowflake
# encodes `(unix_ms - DISCORD_EPOCH_MS) << 22`, so a timestamp maps to the
# smallest snowflake created at that instant — exactly what `min_id`/`max_id`
# search bounds want.
DISCORD_EPOCH_MS = 1420070400000


_SNOWFLAKE_RE = re.compile(r"[0-9]+")


def snowflake(value: str, *, field: str = "id") -> Snowflake:
    """Smart constructor — the only way to produce a Snowflake."""
    if not _SNOWFLAKE_RE.fullmatch(value):
        raise InvalidSnowflake(
            f"{field} must be a numeric Discord snowflake, got: {value!r}"
        )
    return Snowflake(value)


def snowflake_for_time(dt: datetime) -> Snowflake:
    """The lowest snowflake that could have been created at or after `dt`.

    Used as a `min_id` search floor so Discord filters by time server-side.
    Times before the Discord epoch clamp to 0.
    """
    ms = int(dt.timestamp() * 1000) - DISCORD_EPOCH_MS
    return Snowflake(str(max(ms, 0) << 22))


@dataclass(frozen=True, slots=True)
class Guild:
    id: Snowflake
    name: str


@dataclass(frozen=True, slots=True)
class Channel:
    id: Snowflake
    name: str
    type: int
    guild_id: Snowflake


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    content: str
    author_name: str
    channel_id: Snowflake
    timestamp: datetime
    attachments: list[str]
    # Only API-sourced messages (search, mentions) reliably carry the author's
    # snowflake; DOM-scraped reads fill it best-effort from the avatar URL.
    author_id: str | None = None


@dataclass(frozen=True, slots=True)
class Thread:
    id: Snowflake
    name: str
    parent_channel_id: Snowflake
