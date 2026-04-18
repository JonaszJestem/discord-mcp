"""Generic async resource pool.

Lazy: resources are only constructed on first demand, up to `max_size`.
Safe: `release(item, broken=True)` closes the item and removes it from the
pool instead of re-queuing it — preventing the classic "expired session keeps
being handed out" failure mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Generic, Protocol, TypeVar


class Closable(Protocol):
    """Anything that knows how to clean itself up asynchronously."""

    async def close(self) -> None: ...


T = TypeVar("T", bound=Closable)


class BrowserPool(Generic[T]):
    """Bounded async pool with lazy construction and broken-item eviction."""

    def __init__(
        self,
        *,
        factory: Callable[[], Awaitable[T]],
        max_size: int,
    ) -> None:
        if max_size < 1:
            raise ValueError(f"max_size must be >= 1, got {max_size}")
        self._factory = factory
        self._max_size = max_size
        self._idle: asyncio.Queue[T] = asyncio.Queue()
        self._tracked: list[T] = []
        self._create_lock = asyncio.Lock()
        self._closed = False

    @property
    def size(self) -> int:
        """Number of resources currently owned by the pool (in use + idle)."""
        return len(self._tracked)

    async def acquire(self) -> T:
        if self._closed:
            raise RuntimeError("pool is closed")
        try:
            return self._idle.get_nowait()
        except asyncio.QueueEmpty:
            pass

        async with self._create_lock:
            if len(self._tracked) < self._max_size:
                item = await self._factory()
                self._tracked.append(item)
                return item

        return await self._idle.get()

    async def release(self, item: T, *, broken: bool = False) -> None:
        """Return an item to the pool, or discard+close it if broken."""
        if broken or self._closed:
            await self._discard(item)
            return
        await self._idle.put(item)

    async def close_all(self) -> None:
        """Close every resource the pool owns."""
        self._closed = True
        items = list(self._tracked)
        self._tracked.clear()
        # Drain the idle queue so nothing is left waiting.
        while not self._idle.empty():
            try:
                self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
        for item in items:
            try:
                await item.close()
            except Exception:  # pragma: no cover — best-effort shutdown
                pass

    async def _discard(self, item: T) -> None:
        if item in self._tracked:
            self._tracked.remove(item)
        try:
            await item.close()
        except Exception:  # pragma: no cover
            pass
