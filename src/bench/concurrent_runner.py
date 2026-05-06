from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from src.bench.harness_runner import arun_harness_for_sample
from src.bench.manifest import HarnessRecord, Manifest, ManifestStore, SampleRecord

logger = logging.getLogger(__name__)


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


async def run_harness_phase(
    manifest: Manifest,
    store: ManifestStore,
    *,
    concurrency: int,
    frontend_port_base: int,
    run_dir: Path,
    project_root: Path,
    extra_args: list[str],
    log_dir: Path,
) -> None:
    """Drive the harness phase concurrently.

    All store.save() calls happen on the main coroutine, so no lock is needed.
    Per-sample errors are isolated; cancellation propagates to subprocesses
    via process-group signaling in arun_harness_for_sample.
    """
    pending = [s for s in manifest.samples if s.harness.status != "completed"]
    if not pending:
        return

    pool = WorkerPortPool(base=frontend_port_base, size=concurrency)
    sem = asyncio.Semaphore(concurrency)

    # Mark all pending as running, save once. Resume reset_running_to_pending
    # symmetrically reverses this if the run is killed.
    for s in pending:
        s.harness.status = "running"
    store.save(manifest)

    async def run_one(sample: SampleRecord) -> tuple[SampleRecord, HarnessRecord]:
        async with sem:
            port = await pool.acquire()
            try:
                try:
                    record = await arun_harness_for_sample(
                        sample,
                        run_dir=run_dir,
                        project_root=project_root,
                        extra_args=[*extra_args, "--frontend-port", str(port)],
                        log_dir=log_dir,
                    )
                except (KeyboardInterrupt, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    logger.error(
                        "[%s] unexpected exception in worker: %r", sample.id, exc,
                    )
                    record = HarnessRecord(
                        status="errored",
                        workdir=sample.harness.workdir,
                        error=f"unexpected exception in worker: {exc!r}",
                    )
                return sample, record
            finally:
                pool.release(port)

    tasks = [asyncio.create_task(run_one(s)) for s in pending]
    try:
        for fut in asyncio.as_completed(tasks):
            sample, record = await fut
            sample.harness = record
            store.save(manifest)
            cost = record.cost_usd or 0.0
            logger.info(
                "[%s] %s verdict=%s rounds=%s cost=$%.2f",
                sample.id, record.status, record.last_verdict,
                record.rounds, cost,
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
