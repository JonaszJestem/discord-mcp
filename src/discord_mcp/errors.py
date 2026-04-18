"""Error hierarchy for discord-mcp.

All raised errors derive from DiscordMcpError so callers (the MCP server, the
CLI) can catch the whole family. Sub-classes carry a specific cause that
determines the action the user should take.
"""


class DiscordMcpError(Exception):
    """Base class for all discord-mcp errors."""


class ConfigError(DiscordMcpError):
    """Invalid or missing configuration."""


class InvalidSnowflake(DiscordMcpError, ValueError):
    """A value that should be a Discord snowflake isn't."""


class SessionError(DiscordMcpError):
    """Something is wrong with the stored Discord session."""


class SessionMissing(SessionError):
    """No session has been stored yet. User needs to run `discord-mcp login`."""


class SessionCorrupt(SessionError):
    """The on-disk session can't be decrypted."""


class SessionExpired(SessionError):
    """Discord no longer accepts the stored session cookies."""


class KeyringUnavailable(SessionError):
    """The OS keyring backend can't be reached on this system."""
