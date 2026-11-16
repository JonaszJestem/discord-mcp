"""GatewayListener supervision: restart, backoff, session expiry."""

import asyncio
from collections.abc import Callable

from discord_mcp.discord.listener import GatewayListener
from discord_mcp.errors import GatewayStalled, SessionExpired
from discord_mcp.events import EventBuffer
from discord_mcp.models import MessageEvent


class _Driver:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.closed = False

    async def stream_gateway(
        self, on_event: Callable[[MessageEvent], None], *, silence_s: float
    ) -> None:
        raise self.error

    async def close(self) -> None:
        self.closed = True


class _Rig:
    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.drivers: list[_Driver] = []
        self.sleeps: list[float] = []
        self.done = asyncio.Event()

    async def factory(self) -> _Driver:
        driver = _Driver(self.errors.pop(0) if self.errors else GatewayStalled("x"))
        self.drivers.append(driver)
        return driver

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if not self.errors:
            self.done.set()
            await asyncio.Event().wait()


async def _run(rig: _Rig) -> GatewayListener:
    listener = GatewayListener(rig.factory, EventBuffer(), sleep=rig.sleep)
    listener.start()
    await asyncio.wait_for(rig.done.wait(), 1)
    return listener


class TestGatewayListener:
    async def test_restarts_with_exponential_backoff_and_closes_each_driver(self):
        rig = _Rig(GatewayStalled("a"), GatewayStalled("b"), GatewayStalled("c"))
        listener = await _run(rig)
        assert rig.sleeps == [5.0, 10.0, 20.0]
        assert all(d.closed for d in rig.drivers)
        assert listener.status().restarts == 2
        assert listener.status().last_error == "GatewayStalled: c"
        await listener.close()
        assert listener.status().running is False

    async def test_session_expiry_is_flagged(self):
        rig = _Rig(SessionExpired("gone"))
        listener = await _run(rig)
        assert listener.status().session_expired is True
        await listener.close()

    async def test_failure_wakes_waiters(self):
        rig = _Rig(GatewayStalled("a"))
        listener = GatewayListener(rig.factory, EventBuffer(), sleep=rig.sleep)
        stopped = False

        def should_stop() -> bool:
            return stopped

        waiter = asyncio.create_task(
            listener.buffer.wait(
                None,
                match=lambda _: True,
                limit=1,
                timeout_s=5,
                should_stop=should_stop,
            )
        )
        await asyncio.sleep(0)
        stopped = True
        listener.start()
        await asyncio.wait_for(waiter, 1)
        await listener.close()

    async def test_start_is_idempotent(self):
        rig = _Rig()
        listener = GatewayListener(rig.factory, EventBuffer(), sleep=rig.sleep)
        listener.start()
        listener.start()
        await asyncio.wait_for(rig.done.wait(), 1)
        assert len(rig.drivers) == 1
        await listener.close()
