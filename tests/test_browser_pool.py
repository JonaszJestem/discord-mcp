import asyncio

import pytest

from discord_mcp.discord.browser_pool import BrowserPool


class FakeBrowser:
    next_id = 0

    def __init__(self) -> None:
        FakeBrowser.next_id += 1
        self.id = FakeBrowser.next_id
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_counter():
    FakeBrowser.next_id = 0


async def _factory() -> FakeBrowser:
    return FakeBrowser()


class TestBrowserPool:
    async def test_creates_lazily_up_to_max_size(self):
        pool = BrowserPool(factory=_factory, max_size=3)
        a = await pool.acquire()
        b = await pool.acquire()
        assert pool.size == 2
        assert {a.id, b.id} == {1, 2}
        await pool.release(a)
        await pool.release(b)

    async def test_reuses_released_item(self):
        pool = BrowserPool(factory=_factory, max_size=2)
        a = await pool.acquire()
        await pool.release(a)
        b = await pool.acquire()
        assert a.id == b.id
        assert pool.size == 1

    async def test_broken_item_is_closed_and_evicted(self):
        pool = BrowserPool(factory=_factory, max_size=2)
        a = await pool.acquire()
        await pool.release(a, broken=True)
        assert a.closed is True
        assert pool.size == 0
        # Next acquire creates a fresh browser
        b = await pool.acquire()
        assert b.id != a.id

    async def test_blocks_when_at_max_size_and_none_idle(self):
        pool = BrowserPool(factory=_factory, max_size=1)
        a = await pool.acquire()

        async def second_acquire():
            return await pool.acquire()

        task = asyncio.create_task(second_acquire())
        await asyncio.sleep(0.05)
        assert not task.done(), "acquire should block while pool is empty + full"
        await pool.release(a)
        b = await asyncio.wait_for(task, timeout=1.0)
        assert a.id == b.id

    async def test_rejects_invalid_max_size(self):
        with pytest.raises(ValueError):
            BrowserPool(factory=_factory, max_size=0)

    async def test_close_all_closes_every_tracked_item(self):
        pool = BrowserPool(factory=_factory, max_size=3)
        a = await pool.acquire()
        b = await pool.acquire()
        c = await pool.acquire()
        await pool.release(a)
        # b and c still "in use" from pool's perspective — close_all still
        # closes them because the pool owns them.
        await pool.close_all()
        assert all(x.closed for x in (a, b, c))
        assert pool.size == 0

    async def test_acquire_after_close_raises(self):
        pool = BrowserPool(factory=_factory, max_size=2)
        await pool.close_all()
        with pytest.raises(RuntimeError):
            await pool.acquire()
