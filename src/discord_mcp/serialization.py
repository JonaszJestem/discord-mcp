"""Model → JSON-ready dicts. Pure functions, no I/O.

Only the fetch CLI consumes these; the MCP server has its own rendering.
Timestamps are ISO-8601 UTC so a shell consumer can compare them as strings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .models import Channel, Guild, Message, Thread


JsonDict = dict[str, Any]


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat() if dt.tzinfo is None else dt.isoformat()


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
