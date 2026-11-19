"""Decoding and classifying the Discord web client's gateway traffic. No I/O.

The client connects with `compress=zlib-stream`: one zlib context spans the
whole socket, and a payload is complete only when the buffered bytes end with
the Z_SYNC_FLUSH marker. A new socket needs a new decoder.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable
from typing import Any, cast

from ..models import EventKind

GATEWAY_HOST = "gateway.discord.gg"
_ZLIB_SUFFIX = b"\x00\x00\xff\xff"
OP_DISPATCH = 0

RawSink = Callable[[dict[str, Any]], None]
"""Receives every dispatch payload, before any filtering."""


class ZlibStreamDecoder:
    def __init__(self) -> None:
        self._inflate = zlib.decompressobj()
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> dict[str, Any] | None:
        """Return the payload a chunk completes, or None while one is partial."""
        self._buffer.extend(chunk)
        if not self._buffer.endswith(_ZLIB_SUFFIX):
            return None
        raw = self._inflate.decompress(bytes(self._buffer))
        self._buffer.clear()
        payload = json.loads(raw)
        return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


def classify(data: dict[str, Any], self_id: str | None) -> frozenset[EventKind] | None:
    """Kinds for a MESSAGE_CREATE payload; None for the session owner's own messages.

    "mention" means a direct user mention (reply pings included); @everyone
    and role mentions are not counted.
    """
    author = data.get("author")
    author_id = (
        cast(dict[str, Any], author).get("id") if isinstance(author, dict) else None
    )
    if self_id is not None and author_id == self_id:
        return None
    kinds: set[EventKind] = {"message"}
    if not data.get("guild_id"):
        kinds.add("dm")
    if self_id is not None and self_id in _mentioned_ids(data.get("mentions")):
        kinds.add("mention")
    return frozenset(kinds)


def _mentioned_ids(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    ids: set[str] = set()
    for item in cast(list[Any], raw):
        if isinstance(item, dict):
            uid = cast(dict[str, Any], item).get("id")
            if isinstance(uid, str):
                ids.add(uid)
    return ids
