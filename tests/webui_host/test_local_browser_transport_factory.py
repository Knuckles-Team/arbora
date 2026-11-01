"""Actual local WebUI transport composition, without operation authority."""

import inspect

import pytest

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


def test_actual_factory_exposes_distinct_local_boundary():
    from overstory.server import create_agent_web_app

    assert (
        "local_browser_boundary" in inspect.signature(create_agent_web_app).parameters
    )


from tests.security.test_anonymous_mode import local_mode as local_mode
from tests.test_served_engine_native_labels import native_engine as native_engine


def _app(monkeypatch, boundary=None):
    from overstory import oidc_session
    from overstory.host import config
    from overstory.server import create_agent_web_app
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    for field in ("auth_jwt_jwks_uri", "auth_jwt_issuer", "auth_jwt_audience"):
        monkeypatch.setattr(config, field, None, raising=False)
    monkeypatch.setattr(config, "allowed_hosts", "127.0.0.1", raising=False)
    monkeypatch.setattr(oidc_session, "load_settings", lambda: None)
    monkeypatch.setenv("OVERSTORY_ACCESS_LOG_POLICY", "disabled")
    return create_agent_web_app(
        Agent(TestModel()),
        {"get_path": lambda value: value},
        listener_host="127.0.0.1",
        auth_mode="none",
        local_browser_boundary=boundary,
    )


from contextlib import asynccontextmanager


@asynccontextmanager
async def _served(native_engine, tmp_path, monkeypatch, *, enabled=True):
    import asyncio

    import uvicorn

    from arbora.access.action_policy import ActionPolicy
    from arbora.identity.local_browser import LocalBrowserTransport
    from arbora.local_browser_boundary import local_browser_boundary
    from arbora.local_browser_channel import LocalBrowserChannelOwner
    from tests.test_real_boot import _free_port

    engine, session = native_engine
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    path = tmp_path / "explicit-policy.json"
    path.write_text('{"rules": []}')
    owner = LocalBrowserChannelOwner(
        engine, ActionPolicy(engine, policy_path=path), LocalBrowserTransport(origin)
    )
    app = _app(monkeypatch, local_browser_boundary(owner) if enabled else None)
    await engine.browser_control_state._call_async(
        lambda client: client.tenants.create("tenant__local"),
        required_scope="kg:write",
        session=session,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, lifespan="off", log_level="error"
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        yield owner, origin, f"ws://127.0.0.1:{port}/ws/local-browser-admission"
    finally:
        server.should_exit = True
        async with asyncio.timeout(10):
            await task
        await engine.pool.close()


def test_real_factory_native_anonymous_socket(native_engine, tmp_path, monkeypatch):
    import asyncio
    import json

    from websockets.asyncio.client import connect

    async def run():
        async with _served(native_engine, tmp_path, monkeypatch) as (
            owner,
            origin,
            uri,
        ):
            async with connect(uri, origin=origin) as websocket:
                assert json.loads(await websocket.recv()) == {
                    "type": "local_channel_ready",
                    "identity": "anonymous",
                    "operation_authority": False,
                }
                await websocket.send('{"type":"ping"}')
                assert json.loads(await websocket.recv()) == {"type": "pong"}
                assert len(owner._sockets) == 1
            async with asyncio.timeout(5):
                while owner._pending:
                    await asyncio.sleep(0.01)
            assert not owner._sockets

    asyncio.run(run())


@pytest.mark.parametrize(
    "case",
    [
        "default-off",
        "wrong-origin",
        "missing-origin",
        "cookie",
        "bearer",
        "forwarded",
        "query",
        "ordinary-ws",
        "duplicate-origin",
        "duplicate-host",
    ],
)
def test_real_handshake_refuses_unqualified_transport(
    native_engine, tmp_path, monkeypatch, case
):
    import asyncio

    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async def run():
        async with _served(
            native_engine, tmp_path, monkeypatch, enabled=case != "default-off"
        ) as (owner, origin, uri):
            headers = {
                "cookie": [("Cookie", "ordinary=value")],
                "bearer": [("Authorization", "Bearer invalid")],
                "forwarded": [("Forwarded", "for=127.0.0.1")],
                "duplicate-origin": [("Origin", origin)],
                "duplicate-host": [("Host", origin.removeprefix("http://"))],
            }.get(case, [])
            selected_origin = (
                "http://other.invalid" if case == "wrong-origin" else origin
            )
            if case == "missing-origin":
                selected_origin = None
            if case == "query":
                uri += "?document=caller-json"
            if case == "ordinary-ws":
                uri = uri.replace("local-browser-admission", "browser-control")
            with pytest.raises(InvalidStatus):
                async with connect(
                    uri, origin=selected_origin, additional_headers=headers
                ):
                    raise AssertionError("unqualified handshake must refuse")
            assert not owner._sockets and not owner._pending

    asyncio.run(run())


@pytest.mark.parametrize(
    "frame",
    [
        '{"type":"execute"}',
        '{"type":"confirm"}',
        '{"type":"execute","type":"ping"}',
        "x" * 1025,
    ],
)
def test_real_socket_rejects_effect_and_oversize_frames(
    native_engine, tmp_path, monkeypatch, frame
):
    import asyncio

    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    async def run():
        async with _served(native_engine, tmp_path, monkeypatch) as (
            owner,
            origin,
            uri,
        ):
            async with connect(uri, origin=origin) as websocket:
                await websocket.recv()
                await websocket.send(frame)
                with pytest.raises(ConnectionClosed):
                    await websocket.recv()
            assert not owner._sockets

    asyncio.run(run())


def test_real_new_socket_cannot_replay_prior_ready_document(
    native_engine, tmp_path, monkeypatch
):
    import asyncio

    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    async def run():
        async with _served(native_engine, tmp_path, monkeypatch) as (
            owner,
            origin,
            uri,
        ):
            async with connect(uri, origin=origin) as first:
                ready = await first.recv()
                first_id = next(iter(owner._sockets.values())).lease_id
            async with connect(uri, origin=origin) as second:
                await second.recv()
                assert all(
                    record.lease_id != first_id for record in owner._sockets.values()
                )
                await second.send(ready)
                with pytest.raises(ConnectionClosed):
                    await second.recv()

    asyncio.run(run())


def test_real_ordinary_routes_keep_no_auth_refusal(
    native_engine, tmp_path, monkeypatch
):
    import asyncio

    import httpx

    async def run():
        async with _served(native_engine, tmp_path, monkeypatch) as (
            _owner,
            origin,
            _uri,
        ):
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                response = await client.post(
                    "/api/browser/arm",
                    json={"grant": "caller-json"},
                    headers={"Origin": origin},
                )
                assert response.status_code == 403
                assert (await client.get("/health")).status_code == 200

    asyncio.run(run())


def test_secondary_send_failure_does_not_chain_private_boundary_error(
    local_mode, tmp_path, monkeypatch
):
    import asyncio
    import importlib

    from arbora.access.action_policy import ActionPolicy
    from arbora.identity.local_browser import LocalBrowserTransport
    from arbora.local_browser_channel import LocalBrowserChannelOwner
    from arbora.mcp_server.served_engine import ServedEngine

    module = importlib.import_module("arbora.local_browser_boundary")
    engine = ServedEngine(None)
    policy = ActionPolicy(engine, policy_path=tmp_path / "unused.json")
    owner = LocalBrowserChannelOwner(
        engine, policy, LocalBrowserTransport("http://127.0.0.1:8765")
    )

    async def refuse(*args):
        raise RuntimeError("synthetic-private-boundary-detail")

    async def broken_send(message):
        raise ValueError("send failed")

    monkeypatch.setattr(module, "_serve", refuse)
    with pytest.raises(ValueError) as caught:
        asyncio.run(module.local_browser_boundary(owner)({}, None, broken_send))
    assert caught.value.__context__ is None


@pytest.mark.parametrize("stage", ["connect", "accept"])
def test_synthetic_handshake_await_change_cannot_advertise_ready(
    native_engine, tmp_path, stage
):
    """Synthetic ASGI await injection, backed by the actual native owner."""
    import asyncio

    from arbora.access.action_policy import ActionPolicy
    from arbora.local_browser_boundary import local_browser_boundary
    from arbora.local_browser_channel import LocalBrowserChannelOwner
    from tests.browser_control.test_local_browser_channel import _socket
    from tests.browser_control.test_local_browser_transport import _transport

    engine, session = native_engine
    path = tmp_path / "policy.json"
    path.write_text('{"rules": []}')
    owner = LocalBrowserChannelOwner(
        engine, ActionPolicy(engine, policy_path=path), _transport()
    )
    messages = []

    def change():
        path.write_text('{"rules": [], "revision_change": true}')

    async def receive():
        if stage == "connect":
            change()
        return {"type": "websocket.connect"}

    async def send(message):
        messages.append(message)
        if stage == "accept" and message["type"] == "websocket.accept":
            change()

    async def run():
        await engine.browser_control_state._call_async(
            lambda client: client.tenants.create("tenant__local"),
            required_scope="kg:write",
            session=session,
        )
        try:
            await local_browser_boundary(owner)(_socket(), receive, send)
        finally:
            await engine.pool.close()

    asyncio.run(run())
    assert not any(message["type"] == "websocket.send" for message in messages)
    assert messages[-1] == {"type": "websocket.close", "code": 4403}
    assert not owner._sockets and not owner._pending
