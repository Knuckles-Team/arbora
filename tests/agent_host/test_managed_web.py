"""Controlled ASGI lifecycle evidence, not deployed WebUI qualification."""
# spec: ARBORA-HOST-R010.7.2.2.2.1

import asyncio
from dataclasses import asdict, replace
from unittest.mock import Mock

import pytest

from arbora.agent_host import HostInterfaceUnavailable, create_agent_server
from arbora.agent_host.managed_web import ManagedWebApplication
from tests.agent_host.test_runtime_binding import ConstructionOwner


def owner_for(tmp_path, app):
    owner = ConstructionOwner(tmp_path)
    owner.configuration = replace(
        owner.configuration, enable_terminal_ui=False, enable_web_ui=True
    )
    owner.request = replace(
        owner.request,
        interfaces="web",
        configuration_digest=owner.configuration.digest(),
    )
    owner.grant = replace(owner.grant, request=owner.request)
    owner.resources = replace(owner.resources, web_application=app)
    owner.binding = replace(
        owner.binding,
        runtime=owner.resources,
        configuration_digest=owner.request.configuration_digest,
    )
    return owner


def wrapper(owner):
    context = owner.context()
    admitted = context.check_runtime(owner.configuration, owner.resources)
    return ManagedWebApplication(
        context, admitted, owner.configuration, owner.resources
    )


def test_lifecycle_messages_and_borrowed_resources(tmp_path):
    seen, sent = [], []

    async def app(scope, receive, send):
        seen.append(await receive())
        await send({"type": "lifespan.startup.complete"})
        seen.append(await receive())
        await send({"type": "lifespan.shutdown.complete"})

    owner = owner_for(tmp_path, app)
    owner.resources = replace(owner.resources, engine=Mock())
    owner.binding = replace(owner.binding, runtime=owner.resources)
    wrapped = wrapper(owner)

    async def run():
        queue = asyncio.Queue()
        for event in ("lifespan.startup", "lifespan.shutdown"):
            await queue.put({"type": event})

        async def send(message):
            sent.append(message)

        await wrapped({"type": "lifespan"}, queue.get, send)

    asyncio.run(run())
    assert seen == [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    assert sent == [
        {"type": "lifespan.startup.complete"},
        {"type": "lifespan.shutdown.complete"},
    ]
    assert owner.calls == 2
    owner.resources.engine.close.assert_not_called()


@pytest.mark.parametrize("change", ["revoke", "application", "revision"])
def test_changed_owner_refuses_before_application(tmp_path, change):
    app = Mock()
    owner = owner_for(tmp_path, app)
    wrapped = wrapper(owner)
    if change == "revoke":
        owner.revoke = True
    elif change == "application":
        owner.change["runtime"] = replace(owner.resources, web_application=Mock())
    else:
        owner.change["configuration_revision"] = "changed"
    sent = []

    async def run():
        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            sent.append(message)

        await wrapped({"type": "lifespan"}, receive, send)

    asyncio.run(run())
    app.assert_not_called()
    assert sent == [
        {"type": "lifespan.startup.failed", "message": "host admission refused"}
    ]


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_application_failure_and_cancellation_propagate(tmp_path, failure):
    async def app(scope, receive, send):
        assert (await receive())["type"] == "lifespan.startup"
        raise failure()

    wrapped = wrapper(owner_for(tmp_path, app))

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        pytest.fail("unexpected message")

    with pytest.raises(failure):
        asyncio.run(wrapped({"type": "lifespan"}, receive, send))


@pytest.mark.parametrize("port", [9000, 0])
def test_launcher_requires_lifespan_and_uses_supplied_app(tmp_path, monkeypatch, port):
    async def app(scope, receive, send):
        pass

    owner = owner_for(tmp_path, app)
    owner.configuration = replace(owner.configuration, port=port)
    owner.request = replace(
        owner.request, configuration_digest=owner.configuration.digest()
    )
    owner.grant = replace(owner.grant, request=owner.request)
    owner.binding = replace(
        owner.binding, configuration_digest=owner.request.configuration_digest
    )
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    create_agent_server(
        **asdict(owner.configuration),
        host_start=owner.context(),
        host_runtime=owner.resources,
    )
    assert isinstance(run.call_args.args[0], ManagedWebApplication)
    assert run.call_args.kwargs["port"] == port
    assert run.call_args.kwargs["lifespan"] == "on"
    assert run.call_args.kwargs["access_log"] is False


def test_missing_app_keeps_refusal(tmp_path, monkeypatch):
    owner = owner_for(tmp_path, None)
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    with pytest.raises(HostInterfaceUnavailable):
        create_agent_server(
            **asdict(owner.configuration),
            host_start=owner.context(),
            host_runtime=owner.resources,
        )
    run.assert_not_called()


@pytest.mark.parametrize("mode", ["terminal", "both", "logs"])
def test_unsupported_interfaces_do_not_start_server(tmp_path, monkeypatch, mode):
    owner = owner_for(tmp_path, Mock())
    terminal = mode in {"terminal", "both"}
    web = mode != "terminal"
    logs = mode == "logs"
    owner.configuration = replace(
        owner.configuration,
        enable_terminal_ui=terminal,
        enable_web_ui=web,
        enable_web_logs=logs,
    )
    owner.request = replace(
        owner.request,
        interfaces="both" if terminal and web else "terminal" if terminal else "web",
        web_logs=logs,
        configuration_digest=owner.configuration.digest(),
    )
    owner.grant = replace(owner.grant, request=owner.request)
    owner.binding = replace(
        owner.binding, configuration_digest=owner.request.configuration_digest
    )
    run = Mock()
    monkeypatch.setattr("uvicorn.run", run)
    with pytest.raises(HostInterfaceUnavailable):
        create_agent_server(
            **asdict(owner.configuration),
            host_start=owner.context(),
            host_runtime=owner.resources,
        )
    run.assert_not_called()


def test_real_asgi_http_preserves_application_boundary(tmp_path):
    import httpx
    from fastapi import FastAPI, Header, HTTPException

    app = FastAPI()

    @app.get("/private")
    async def private(authorization: str | None = Header(default=None)):
        if authorization != "Bearer controlled-test":
            raise HTTPException(status_code=403)
        return {"ok": True}

    wrapped = wrapper(owner_for(tmp_path, app))

    async def run():
        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            assert (await client.get("/private")).status_code == 403
            assert (
                await client.get(
                    "/private", headers={"Authorization": "Bearer controlled-test"}
                )
            ).json() == {"ok": True}

    asyncio.run(run())


def test_restart_revalidates_and_denies_revoked_owner(tmp_path):
    starts = []

    async def app(scope, receive, send):
        starts.append(await receive())
        await send({"type": "lifespan.startup.complete"})

    owner = owner_for(tmp_path, app)
    wrapped = wrapper(owner)
    sent = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        sent.append(message)

    asyncio.run(wrapped({"type": "lifespan"}, receive, send))
    owner.revoke = True
    asyncio.run(wrapped({"type": "lifespan"}, receive, send))
    assert len(starts) == 1
    assert sent[-1]["type"] == "lifespan.startup.failed"


def test_uvicorn_requires_successful_lifespan_without_binding_listener(tmp_path):
    import uvicorn
    from uvicorn.lifespan.on import LifespanOn

    async def refused(scope, receive, send):
        await receive()
        await send({"type": "lifespan.startup.failed", "message": "controlled refusal"})

    owner = owner_for(tmp_path, refused)
    wrapped = wrapper(owner)
    config = uvicorn.Config(wrapped, lifespan="on", access_log=False, log_config=None)
    lifespan = LifespanOn(config)
    asyncio.run(lifespan.startup())
    assert lifespan.should_exit
    assert lifespan.startup_failed


def test_request_scope_and_channels_are_forwarded_by_identity(tmp_path):
    observed = []

    async def app(scope, receive, send):
        observed.append((scope, receive, send))

    wrapped = wrapper(owner_for(tmp_path, app))
    scope = {"type": "websocket", "path": "/existing-boundary"}

    async def receive():
        raise AssertionError("wrapper must not consume request messages")

    async def send(message):
        raise AssertionError("wrapper must not emit request messages")

    asyncio.run(wrapped(scope, receive, send))
    assert observed[0][0] is scope
    assert observed[0][1] is receive
    assert observed[0][2] is send
