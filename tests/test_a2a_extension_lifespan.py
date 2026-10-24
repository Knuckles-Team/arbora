"""Real AnyIO lifespan scopes survive FastMCP's shielded extension teardown."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
from fastmcp import FastMCP

from arbora.host_bindings import A2AAppExtension

pytestmark = [pytest.mark.asyncio, pytest.mark.spec("ARBORA-FLEET-R015")]


def extension(lifespan: Any) -> A2AAppExtension:
    return A2AAppExtension(
        SimpleNamespace(router=SimpleNamespace(lifespan_context=lifespan))
    )


def leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in leaves(child)]
    return [exc]


async def test_fastmcp_teardown_joins_real_anyio_worker_in_owner_task() -> None:
    entered = []
    worker_stopped = asyncio.Event()

    async def worker() -> None:
        try:
            await anyio.sleep_forever()
        finally:
            worker_stopped.set()

    @asynccontextmanager
    async def lifespan(app: Any):
        entered.append(asyncio.current_task())
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker)
            try:
                yield
            finally:
                assert asyncio.current_task() is entered[0]
                tasks.cancel_scope.cancel()

    host = FastMCP("a2a-lifespan-owner-regression")
    host.add_extension(extension(lifespan))
    async with host._lifespan_manager():
        assert entered[0] is not asyncio.current_task()
        assert not worker_stopped.is_set()
    assert worker_stopped.is_set()


async def test_startup_failure_propagates_without_entering_server_body() -> None:
    @asynccontextmanager
    async def lifespan(app: Any):
        raise ValueError("startup failed")
        yield

    with pytest.raises(ExceptionGroup) as failed:
        async with extension(lifespan).lifespan():
            pytest.fail("body must not start before SDK startup")
    assert any(isinstance(exc, ValueError) for exc in leaves(failed.value))


async def test_cancelled_startup_joins_sdk_cleanup() -> None:
    starting = asyncio.Event()
    cleaned = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: Any):
        starting.set()
        try:
            await asyncio.Event().wait()
            yield
        finally:
            cleaned.set()

    async def serve() -> None:
        async with extension(lifespan).lifespan():
            pytest.fail("startup is still pending")

    task = asyncio.create_task(serve())
    await starting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_background_sdk_failure_interrupts_serving_and_propagates() -> None:
    fail = asyncio.Event()
    cleaned = asyncio.Event()

    async def worker() -> None:
        await fail.wait()
        raise ValueError("worker failed")

    @asynccontextmanager
    async def lifespan(app: Any):
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(worker)
                yield
        finally:
            cleaned.set()

    with pytest.raises(ExceptionGroup) as failed:
        async with extension(lifespan).lifespan():
            fail.set()
            await asyncio.Event().wait()
    assert cleaned.is_set()
    assert any(isinstance(exc, ValueError) for exc in leaves(failed.value))


async def test_teardown_failure_propagates() -> None:
    @asynccontextmanager
    async def lifespan(app: Any):
        yield
        raise ValueError("teardown failed")

    with pytest.raises(ExceptionGroup) as failed:
        async with extension(lifespan).lifespan():
            pass
    assert any(isinstance(exc, ValueError) for exc in leaves(failed.value))


async def test_cancel_during_shutdown_joins_sdk_cleanup() -> None:
    stopping = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: Any):
        yield
        stopping.set()
        await release.wait()
        cleaned.set()

    async def serve() -> None:
        async with extension(lifespan).lifespan():
            pass

    task = asyncio.create_task(serve())
    await stopping.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
