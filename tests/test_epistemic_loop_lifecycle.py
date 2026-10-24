"""ARBORA-HOST-R001.2.3: temporary request-loop transport ownership."""

import asyncio
import contextlib
from typing import Any

from arbora.epistemic import EpistemicClientPool
from arbora.mcp_server.served_engine import _run_blocking
from tests.test_epistemic_adapter import Client, context


class ReaderClient(Client):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(kwargs["graph_name"], kwargs["verified_context"])
        self.loop = asyncio.get_running_loop()
        self.reader = asyncio.create_task(asyncio.Event().wait())
        self.close_count = 0

    async def close(self) -> None:
        assert asyncio.get_running_loop() is self.loop
        self.close_count += 1
        self.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.reader
        self.closed = True


def test_temporary_loop_closes_transport_without_displacing_serving_reader() -> None:
    clients: list[ReaderClient] = []

    async def connect(**kwargs: Any) -> ReaderClient:
        client = ReaderClient(**kwargs)
        clients.append(client)
        return client

    async def exercise() -> None:
        pool = EpistemicClientPool(connect)
        caller = context("graph:read")
        serving = await pool.connected_client(caller, "graph")

        async def request() -> str:
            temporary = await pool.connected_client(caller, "graph")
            assert temporary is not serving
            return "reply"

        try:
            for _ in range(3):
                assert _run_blocking(request) == "reply"
                assert clients[-1].closed
                assert clients[-1].reader.done()
                assert await pool.connected_client(caller, "graph") is serving
            assert not clients[0].reader.done()
        finally:
            await pool.close()
            await pool.close()
        assert all(client.close_count == 1 for client in clients)

    asyncio.run(exercise())


def test_failed_and_cancelled_requests_close_before_loop_exit() -> None:
    clients: list[ReaderClient] = []

    async def connect(**kwargs: Any) -> ReaderClient:
        client = ReaderClient(**kwargs)
        clients.append(client)
        return client

    pool = EpistemicClientPool(connect)
    caller = context("graph:read")

    async def failed() -> None:
        await pool.connected_client(caller, "graph")
        raise ValueError("request refused")

    async def cancelled() -> None:
        await pool.connected_client(caller, "graph")
        raise asyncio.CancelledError

    import pytest

    with pytest.raises(ValueError, match="request refused"):
        _run_blocking(failed)
    with pytest.raises(asyncio.CancelledError):
        _run_blocking(cancelled)
    assert all(client.closed and client.reader.done() for client in clients)
    assert all(client.close_count == 1 for client in clients)
    assert pool.graph_count == 0


def test_cancellation_during_cleanup_waits_for_reader_and_writer() -> None:
    from arbora.epistemic import temporary_client_scope

    async def exercise() -> None:
        closing = asyncio.Event()
        release = asyncio.Event()
        clients: list[ReaderClient] = []

        class SlowClose(ReaderClient):
            async def close(self) -> None:
                closing.set()
                await release.wait()
                await super().close()

        async def connect(**kwargs: Any) -> ReaderClient:
            client = SlowClose(**kwargs)
            clients.append(client)
            return client

        pool = EpistemicClientPool(connect)

        async def request() -> None:
            async with temporary_client_scope():
                await pool.connected_client(context("graph:read"), "graph")

        task = asyncio.create_task(request())
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()
        assert clients[0].reader.done()
        assert clients[0].close_count == 1
        await pool.close()
        await pool.close()
        assert clients[0].close_count == 1

    asyncio.run(exercise())


def test_temporary_scope_reuses_client_but_refuses_cross_tenant() -> None:
    from dataclasses import replace

    import pytest

    from arbora.epistemic import TenantGraphBindingError

    clients: list[ReaderClient] = []

    async def connect(**kwargs: Any) -> ReaderClient:
        client = ReaderClient(**kwargs)
        clients.append(client)
        return client

    pool = EpistemicClientPool(connect)
    caller = context("graph:read")

    async def request() -> None:
        client = await pool.connected_client(caller, "graph")
        assert await pool.connected_client(caller, "graph") is client
        with pytest.raises(TenantGraphBindingError):
            await pool.connected_client(replace(caller, tenant="other"), "graph")

    _run_blocking(request)
    assert len(clients) == 1
    assert clients[0].closed and clients[0].reader.done()
    assert pool.graph_count == 0


def test_cleanup_reports_failure_after_closing_remaining_clients() -> None:
    import pytest

    clients: list[ReaderClient] = []

    class FailingClose(ReaderClient):
        async def close(self) -> None:
            await super().close()
            raise ValueError("writer cleanup failed")

    async def connect(**kwargs: Any) -> ReaderClient:
        cls = FailingClose if kwargs["graph_name"] == "first" else ReaderClient
        client = cls(**kwargs)
        clients.append(client)
        return client

    pool = EpistemicClientPool(connect)

    async def request() -> None:
        await pool.connected_client(context("graph:read"), "first")
        await pool.connected_client(context("graph:read"), "second")

    with pytest.raises(ExceptionGroup, match="temporary client cleanup failed"):
        _run_blocking(request)
    assert all(client.closed and client.reader.done() for client in clients)


def test_unscoped_foreign_loop_refuses_without_displacing_live_client() -> None:
    import pytest

    async def connect(**kwargs: Any) -> ReaderClient:
        return ReaderClient(**kwargs)

    async def exercise() -> None:
        pool = EpistemicClientPool(connect)
        caller = context("graph:read")
        client = await pool.connected_client(caller, "graph")

        def foreign() -> None:
            with pytest.raises(RuntimeError, match="another event loop"):
                asyncio.run(pool.connected_client(caller, "graph"))

        await asyncio.to_thread(foreign)
        assert await pool.connected_client(caller, "graph") is client
        await pool.close()

    asyncio.run(exercise())
