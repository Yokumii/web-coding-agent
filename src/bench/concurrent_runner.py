from __future__ import annotations

import asyncio


class WorkerPortPool:
    """Bounded async pool of frontend dev-server ports.

    Constructed with a contiguous range [base, base+size). Each `acquire()`
    blocks if the pool is drained; `release(port)` returns a port to the queue.
    """

    def __init__(self, *, base: int, size: int) -> None:
        if size < 1:
            raise ValueError(f"WorkerPortPool size must be >= 1, got {size}")
        self._q: asyncio.Queue[int] = asyncio.Queue(maxsize=size)
        for i in range(size):
            self._q.put_nowait(base + i)

    async def acquire(self) -> int:
        return await self._q.get()

    def release(self, port: int) -> None:
        self._q.put_nowait(port)
