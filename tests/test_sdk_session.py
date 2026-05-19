from __future__ import annotations

import asyncio

import pytest

from src.utils.sdk_session import safe_sdk_session


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_passes_through_normal_exit():
    async with safe_sdk_session(phase_name="test"):
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_suppresses_leaked_cancellation_on_exit():
    async with safe_sdk_session(phase_name="test"):
        await asyncio.sleep(0)
        # Simulate the SDK queuing a parent-task cancellation that lands
        # after the with-body completes.
        asyncio.current_task().cancel()
    # Should NOT raise — leaked cancellation absorbed on exit.


@pytest.mark.anyio
async def test_propagates_real_exception_inside_block():
    with pytest.raises(ValueError, match="boom"):
        async with safe_sdk_session(phase_name="test"):
            raise ValueError("boom")


@pytest.mark.anyio
async def test_cancellation_inside_block_is_absorbed_when_uncancellable():
    """When task.cancel() is called inside the block and a subsequent
    await surfaces CancelledError, safe_sdk_session uncancels and
    continues — matching the leaked-cancellation pattern emitted by
    SDK-backed cleanup paths (e.g. app_stack.close())."""

    async def cancel_self():
        asyncio.current_task().cancel()
        await asyncio.sleep(0)

    async with safe_sdk_session(phase_name="test"):
        await cancel_self()
    # Should NOT raise — leak absorbed.


@pytest.mark.anyio
async def test_unqueued_cancellation_propagates():
    """If CancelledError surfaces without a corresponding pending
    cancellation count (we can't uncancel it), propagate."""

    async def raise_cancel():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        async with safe_sdk_session(phase_name="test"):
            await raise_cancel()


@pytest.mark.anyio
async def test_delayed_cancellation_after_body_is_absorbed():
    """A cancellation queued by `call_soon` after the body returns must
    be absorbed by the exit drain, matching the real SDK leak pattern."""

    async with safe_sdk_session(phase_name="test"):
        task = asyncio.current_task()
        asyncio.get_running_loop().call_soon(task.cancel)
        # Body completes synchronously after scheduling.
    # No exception expected; one extra tick is enough for `call_soon`
    # to land.


@pytest.mark.anyio
async def test_multiple_leaked_cancellations_absorbed():
    async with safe_sdk_session(phase_name="test"):
        task = asyncio.current_task()
        task.cancel()
        task.cancel()
    # No exception expected.


@pytest.mark.anyio
async def test_repeated_leaked_cancellations_do_not_stall_exit():
    loop = asyncio.get_running_loop()

    async def run():
        task = asyncio.current_task()
        active = {"value": True}

        def spam_cancel():
            if not active["value"]:
                return
            task.cancel()
            loop.call_soon(spam_cancel)

        try:
            async with safe_sdk_session(phase_name="test"):
                loop.call_soon(spam_cancel)
        finally:
            active["value"] = False

    await asyncio.wait_for(asyncio.create_task(run()), timeout=1.0)
    await asyncio.sleep(0)
