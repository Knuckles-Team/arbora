"""Real event loops and real pool admission, with synthetic wire transport."""

import asyncio
import contextvars
import threading
from contextlib import contextmanager

import pytest

from arbora.epistemic import ClientContext, EpistemicClientPool
from arbora.mcp_server.browser_composition import _IdentityClient
from arbora.mcp_server.browser_dispatch import _on_owner

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.5.2")


@pytest.fixture
def owner_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def serve():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=serve)
    thread.start()
    ready.wait()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()


def context(policy="one", tenant="fixture-tenant"):
    return ClientContext(
        principal="broker",
        tenant=tenant,
        audience="fixture",
        agent_id="broker",
        roles=("identity:admin",),
        scopes=("identity:admin",),
        policy_version=policy,
    )


def test_real_pool_requires_owner_loop_and_fresh_broker_claims(owner_loop):
    current = contextvars.ContextVar("fixture_claims", default=None)
    current.set({"principal": "ambient-other"})
    observed = []

    class Wire:
        @contextmanager
        def use_verified_context(self, claims):
            token = current.set(claims)
            try:
                yield
            finally:
                current.reset(token)

        async def _send(self, *args, **kwargs):
            observed.append((asyncio.get_running_loop(), current.get()))

        async def close(self):
            pass

    async def connect(**kwargs):
        return Wire()

    pool = EpistemicClientPool(connect)
    asyncio.run_coroutine_threadsafe(
        pool.connected_client(context(), "fixture-graph"), owner_loop
    ).result()

    async def scenario():
        with pytest.raises(RuntimeError, match="another event loop"):
            await pool.connected_client(context(), "fixture-graph")
        policy = ["one"]

        async def client():
            return await pool.connected_client(context(policy[0]), "fixture-graph")

        broker = _IdentityClient(client, lambda: context(policy[0]).to_claims())

        async def app(scope, receive, send):
            await receive()
            await broker._send("Identity", {}, "fixture-graph")
            policy[0] = "two"
            await broker._send("Identity", {}, "fixture-graph")
            await send({"type": "http.response.body", "body": b"ok"})

        source = asyncio.get_running_loop()

        async def receive():
            assert asyncio.get_running_loop() is source
            return {"type": "http.request", "body": b""}

        async def send(message):
            assert asyncio.get_running_loop() is source

        await _on_owner(app, {}, receive, send, owner_loop)
        assert current.get()["principal"] == "ambient-other"
        assert [claims["policy_version"] for _, claims in observed] == ["one", "two"]
        assert all(
            loop is owner_loop and claims["principal"] == "broker"
            for loop, claims in observed
        )

    asyncio.run(scenario())


def test_cancellation_waits_for_actual_owner_cleanup(owner_loop):
    started, cleaned, released = threading.Event(), threading.Event(), []

    async def app(scope, receive, send):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.02)
            cleaned.set()

    async def unused(*args):
        pytest.fail("unexpected IO")

    async def scenario():
        task = asyncio.create_task(
            _on_owner(
                app,
                {},
                unused,
                unused,
                owner_loop,
                release=lambda: released.append(cleaned.is_set()),
            )
        )
        await asyncio.to_thread(started.wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set() and released == [True]

    asyncio.run(scenario())


def test_capacity_rejects_before_owner_submission():
    from arbora.mcp_server import browser_dispatch

    async def scenario():
        messages = []

        async def send(message):
            messages.append(message)

        async def forbidden(*args):
            pytest.fail("over-capacity request was dispatched")

        acquired = 0
        try:
            while browser_dispatch._capacity.acquire(blocking=False):
                acquired += 1
            await browser_dispatch.dispatch_browser_request(
                forbidden, {"type": "http"}, forbidden, send
            )
        finally:
            for _ in range(acquired):
                browser_dispatch._capacity.release()
        assert messages[0]["status"] == 503

    asyncio.run(scenario())


def test_missing_owner_fails_closed(monkeypatch):
    from arbora.fleet import shared_multiplexer
    from arbora.mcp_server.browser_dispatch import dispatch_browser_request

    async def unavailable(operation):
        raise shared_multiplexer.ServedMultiplexerBindingError("not started")

    monkeypatch.setattr(shared_multiplexer, "run_on_served_multiplexer", unavailable)

    async def scenario():
        messages = []

        async def send(message):
            messages.append(message)

        async def forbidden(*args):
            pytest.fail("unavailable owner dispatched")

        await dispatch_browser_request(forbidden, {"type": "http"}, forbidden, send)
        assert messages[0]["status"] == 503

    asyncio.run(scenario())


def test_owner_task_construction_failure_completes_and_releases(owner_loop):
    released = []

    def app(*args):
        raise RuntimeError("fixture construction failed")

    async def scenario():
        with pytest.raises(RuntimeError, match="construction failed"):
            await asyncio.wait_for(
                _on_owner(
                    app,
                    {},
                    None,
                    None,
                    owner_loop,
                    release=lambda: released.append(True),
                ),
                timeout=1,
            )
        assert released == [True]

    asyncio.run(scenario())
