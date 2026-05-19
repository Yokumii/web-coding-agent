"""吸收 claude-agent-sdk 向父任务泄漏的延迟取消信号。"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from src.utils.logger import get_logger

logger = get_logger(__name__)
_DRAIN_TIMEOUT_SECS = 0.5


def _clear_current_task_cancellation() -> int:
    """尽力清空当前任务上的挂起取消标记，并返回清理次数。"""
    task = asyncio.current_task()
    if task is None:
        return 0
    cancelling = getattr(task, "cancelling", None)
    uncancel = getattr(task, "uncancel", None)
    if cancelling is None or uncancel is None:
        return 0
    cleared = 0
    while cancelling():
        uncancel()
        cleared += 1
    return cleared


@asynccontextmanager
async def safe_sdk_session(*, phase_name: str) -> AsyncIterator[None]:
    """包裹单个 SDK 阶段，并在退出时吸收延迟泄漏的取消信号。"""
    suppressed = 0
    try:
        yield
    except asyncio.CancelledError:
        cleared = _clear_current_task_cancellation()
        if cleared == 0:
            # 当前任务上没有可清理的挂起取消，说明这次取消来自更高层控制流。
            raise
        suppressed += cleared

    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECS
    drain_timed_out = False
    while True:
        try:
            await asyncio.shield(asyncio.sleep(0))
            break
        except asyncio.CancelledError:
            cleared = _clear_current_task_cancellation()
            suppressed += max(cleared, 1)
            if time.monotonic() >= deadline:
                drain_timed_out = True
                break

    if suppressed > 0:
        suffix = " Drain timed out; continuing." if drain_timed_out else ""
        logger.warning(
            f"[bold yellow]Suppressed leaked cancellation after {phase_name}.[/] "
            f"count={suppressed}.{suffix}"
        )
