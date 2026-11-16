"""Model → JSON-ready dicts. Pure functions, no I/O.

Only the fetch CLI consumes these; the MCP server has its own rendering.
Timestamps carry an explicit offset so a shell consumer can compare them as
strings and a Python one can subtract them from an aware `now`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import Channel, Guild, Message, Thread


JsonDict = dict[str, Any]


def _iso(dt: datetime) -> str:
    """Naive input is read as UTC, not as local time: a consumer subtracting
    this from `now(utc)` would otherwise mis-age the message by the offset."""
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def guild_to_dict(guild: Guild) -> JsonDict:
    return {"id": guild.id, "name": guild.name}


def channel_to_dict(channel: Channel) -> JsonDict:
    return {
        "id": channel.id,
        "name": channel.name,
        "type": channel.type,
        "guild_id": channel.guild_id,
    }


def thread_to_dict(thread: Thread) -> JsonDict:
    return {
        "id": thread.id,
        "name": thread.name,
        "parent_channel_id": thread.parent_channel_id,
        "last_activity": _iso(thread.last_activity) if thread.last_activity else None,
    }


def message_to_dict(message: Message) -> JsonDict:
    reply = message.reply_to
    return {
        "id": message.id,
        "channel_id": message.channel_id,
        "author_id": message.author_id,
        "author_name": message.author_name,
        "timestamp": _iso(message.timestamp),
        "content": message.content,
        "attachments": list(message.attachments),
        "mention_names": list(message.mention_names),
        "reply_to": (
            {
                "message_id": reply.message_id,
                "author_id": reply.author_id,
                "author_name": reply.author_name,
                "content": reply.content,
            }
            if reply
            else None
        ),
    }
