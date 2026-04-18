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


_SNOWFLAKE_RE = re.compile(r"[0-9]+")


def snowflake(value: str, *, field: str = "id") -> Snowflake:
    """Smart constructor — the only way to produce a Snowflake."""
    if not _SNOWFLAKE_RE.fullmatch(value):
        raise InvalidSnowflake(
            f"{field} must be a numeric Discord snowflake, got: {value!r}"
        )
    return Snowflake(value)


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


@dataclass(frozen=True, slots=True)
class Thread:
    id: Snowflake
    name: str
    parent_channel_id: Snowflake
