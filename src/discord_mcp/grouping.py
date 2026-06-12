"""Token-efficient grouping of messages for LLM context — pure, no I/O.

Takes domain `Message` objects and returns JSON-serialisable structures.
Two grouping strategies:

- `group_conversations`: channel → time-gap bursts (consecutive messages with
  small gaps cluster into one "conversation"), collapsing a 50-message debug
  thread into a single block instead of 50 disconnected rows.
- `group_channels`: one bucket per channel, messages listed within.

Repeated metadata (channel, author when single-author) is hoisted out of each
message; verbatim content is never altered.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .models import Message

CONVERSATION_GAP_MINUTES = 10
_REPLY_PREVIEW_CHARS = 140


def _truncate(text: str, limit: int = _REPLY_PREVIEW_CHARS) -> str:
    flat = text.replace("\n", " ").strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def message_view(
    m: Message, *, show_author: bool, include_mentions: bool
) -> dict[str, Any]:
    """Compact, verbatim view of a single message for grouped output."""
    view: dict[str, Any] = {"t": m.timestamp.strftime("%H:%M"), "c": m.content}
    if show_author:
        view["by"] = m.author_name
    if m.reply_to is not None:
        view["re"] = {
            "who": m.reply_to.author_name,
            "c": _truncate(m.reply_to.content),
        }
    if include_mentions and m.mention_names:
        view["@"] = list(m.mention_names)
    if m.attachments:
        view["attachments"] = len(m.attachments)
    return view


def _span(start: Any, end: Any) -> str:
    if start.date() == end.date():
        return f"{start:%b %d %H:%M}–{end:%H:%M}"
    return f"{start:%b %d %H:%M}–{end:%b %d %H:%M}"


def _by_channel(messages: list[Message]) -> dict[str, list[Message]]:
    grouped: dict[str, list[Message]] = {}
    for m in messages:
        grouped.setdefault(m.channel_id, []).append(m)
    return grouped


def split_bursts(
    messages: list[Message], *, gap_minutes: int = CONVERSATION_GAP_MINUTES
) -> list[list[Message]]:
    """Split one channel's messages into chronological time-gap bursts.

    A burst breaks whenever consecutive messages are more than `gap_minutes`
    apart. Input may be in any order; each burst comes out chronological.
    """
    gap = timedelta(minutes=gap_minutes)
    ordered = sorted(messages, key=lambda m: m.timestamp)
    bursts: list[list[Message]] = []
    current: list[Message] = []
    for m in ordered:
        if current and m.timestamp - current[-1].timestamp > gap:
            bursts.append(current)
            current = []
        current.append(m)
    if current:
        bursts.append(current)
    return bursts


def group_conversations(
    messages: list[Message],
    *,
    show_author: bool,
    include_mentions: bool,
    gap_minutes: int = CONVERSATION_GAP_MINUTES,
) -> list[dict[str, Any]]:
    """Group into channel → time-gap bursts.

    Channels ordered newest-activity first; bursts newest first; messages
    within a burst chronological so each reads top-to-bottom.
    """
    ranked: list[tuple[Any, dict[str, Any]]] = []
    for channel_id, msgs in _by_channel(messages).items():
        ordered = sorted(msgs, key=lambda m: m.timestamp)
        bursts = split_bursts(msgs, gap_minutes=gap_minutes)
        burst_views = [
            {
                "span": _span(b[0].timestamp, b[-1].timestamp),
                "count": len(b),
                "messages": [
                    message_view(
                        m, show_author=show_author, include_mentions=include_mentions
                    )
                    for m in b
                ],
            }
            for b in bursts
        ]
        latest = ordered[-1].timestamp
        ranked.append(
            (latest, {"channel_id": channel_id, "bursts": list(reversed(burst_views))})
        )
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in ranked]


def group_channels(
    messages: list[Message], *, show_author: bool, include_mentions: bool
) -> list[dict[str, Any]]:
    """One bucket per channel (newest-activity first), messages newest first."""
    ranked: list[tuple[Any, dict[str, Any]]] = []
    for channel_id, msgs in _by_channel(messages).items():
        ordered = sorted(msgs, key=lambda m: m.timestamp, reverse=True)
        ranked.append(
            (
                ordered[0].timestamp,
                {
                    "channel_id": channel_id,
                    "count": len(ordered),
                    "messages": [
                        message_view(
                            m,
                            show_author=show_author,
                            include_mentions=include_mentions,
                        )
                        for m in ordered
                    ],
                },
            )
        )
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in ranked]
