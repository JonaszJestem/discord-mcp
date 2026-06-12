"""Configuration: one Config, loaded once at the entry point.

No other module in the codebase calls os.getenv. Everything downstream takes
Config (or a field of it) as a dependency.
"""

from __future__ import annotations

import os
import pathlib as pl
from dataclasses import dataclass

from dotenv import load_dotenv


_KEYRING_SERVICE = "discord-mcp"
_KEYRING_USER = "session-key"


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime configuration for the MCP server and the CLI."""

    session_path: pl.Path
    cache_path: pl.Path
    keyring_service: str
    keyring_user: str
    headless: bool
    read_only: bool
    pool_size: int

    @classmethod
    def load(cls) -> "Config":
        env_file = pl.Path(".env")
        if env_file.exists():
            load_dotenv(env_file)

        return cls(
            session_path=_config_file("session.enc"),
            cache_path=_config_file("discovery.json"),
            keyring_service=_KEYRING_SERVICE,
            keyring_user=_KEYRING_USER,
            headless=_env_bool("DISCORD_HEADLESS", default=True),
            read_only=_env_bool("DISCORD_READ_ONLY", default=True),
            pool_size=_env_int("DISCORD_POOL_SIZE", default=4, minimum=1),
        )


def _config_file(name: str) -> pl.Path:
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = pl.Path(xdg) if xdg else pl.Path.home() / ".config"
    return base / "discord-mcp" / name


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, *, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)
