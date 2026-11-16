"""GatewayListener: keeps one dedicated browser streaming gateway events.

The listener's driver lives outside the request pool so a long-lived socket
never blocks or is evicted by tool calls. It restarts with backoff when the
page dies or the socket goes silent.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from ..errors import SessionExpired
from ..events import EventBuffer
from ..logger import logger
from ..models import MessageEvent

SILENCE_S = 180.0
_BACKOFF_START_S = 5.0
_BACKOFF_MAX_S = 300.0
# A run this long counts as healthy and resets the backoff.
_HEALTHY_RUN_S = 60.0


class GatewayDriver(Protocol):
    async def stream_gateway(
        self, on_event: Callable[[MessageEvent], None], *, silence_s: float
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ListenerStatus:
    running: bool
    connected: bool
    restarts: int
    last_error: str | None
    session_expired: bool


class GatewayListener:
    def __init__(
        self,
        driver_factory: Callable[[], Awaitable[GatewayDriver]],
        buffer: EventBuffer,
        *,
        silence_s: float = SILENCE_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._factory = driver_factory
        self.buffer = buffer
        self._silence_s = silence_s
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._connected = False
        self._restarts = 0
        self._last_error: str | None = None
        self._session_expired = False

    def start(self) -> None:
        if self._session_expired:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="gateway-listener")

    def status(self) -> ListenerStatus:
        return ListenerStatus(
            running=self._task is not None and not self._task.done(),
            connected=self._connected,
            restarts=self._restarts,
            last_error=self._last_error,
            session_expired=self._session_expired,
        )

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        # asyncio.wait, unlike awaiting the task, lets the caller's own
        # cancellation propagate instead of swallowing it with the task's.
        await asyncio.wait([self._task])
        self._task = None

    async def _run(self) -> None:
        backoff = _BACKOFF_START_S
        while True:
            started = time.monotonic()
            try:
                await self._stream_once()
            except SessionExpired as e:
                # The server holds the session it started with; retrying only
                # reloads a logged-out page. A fresh login needs a restart.
                self._session_expired = True
                self._record_failure(e)
                return
            except Exception as e:
                self._record_failure(e)
            if time.monotonic() - started >= _HEALTHY_RUN_S:
                backoff = _BACKOFF_START_S
            await self._sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
            self._restarts += 1

    async def _stream_once(self) -> None:
        driver = await self._factory()
        self._connected = True
        try:
            await driver.stream_gateway(self.buffer.publish, silence_s=self._silence_s)
        finally:
            self._connected = False
            try:
                await driver.close()
            except Exception as e:
                logger.debug(f"Error closing gateway driver: {e}")

    def _record_failure(self, exc: Exception) -> None:
        self._last_error = f"{type(exc).__name__}: {exc}"
        logger.warning(f"Gateway listener stopped: {self._last_error}")
        # Wake waiters so a long wait can report a dead session promptly.
        self.buffer.wake()
