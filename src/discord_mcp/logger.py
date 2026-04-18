"""Module-level logger.

Writes to stderr (never stdout) so MCP stdio framing isn't corrupted. The
default level is INFO; set `DISCORD_MCP_LOG_LEVEL=DEBUG` for verbose output.
"""

import logging
import os
import sys


def _setup_logger(name: str = "discord_mcp") -> logging.Logger:
    logger = logging.getLogger(name)
    level_name = os.getenv("DISCORD_MCP_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"
        )
    )
    logger.addHandler(handler)
    return logger


logger = _setup_logger()
