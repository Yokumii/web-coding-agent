from __future__ import annotations

import asyncio

import pytest

from src.bench.concurrent_runner import WorkerPortPool


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_port_pool_serves_distinct_ports() -> None:
    pool = WorkerPortPool(base=5173, size=4)
    seen = {await pool.acquire() for _ in range(4)}
    assert seen == {5173, 5174, 5175, 5176}


@pytest.mark.anyio
async def test_port_pool_blocks_when_exhausted_until_release() -> None:
    pool = WorkerPortPool(base=5173, size=2)
    a = await pool.acquire()
    b = await pool.acquire()
    # Now drained; the next acquire should block.
    blocked = asyncio.create_task(pool.acquire())
    await asyncio.sleep(0.05)
    assert not blocked.done()
    pool.release(a)
    got = await asyncio.wait_for(blocked, timeout=1.0)
    assert got == a
    pool.release(b)
    pool.release(got)


def test_port_pool_rejects_size_zero_or_negative() -> None:
    with pytest.raises(ValueError):
        WorkerPortPool(base=5173, size=0)
    with pytest.raises(ValueError):
        WorkerPortPool(base=5173, size=-1)
