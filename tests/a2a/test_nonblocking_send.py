"""Non-blocking ``message/send`` over the served HTTP A2A route (ARBORA-A2A-R001.6)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from agent_utilities import host_ports

from arbora.core.settings import runtime_config
from arbora.mcp_server import runtime as mcp_runtime
from tests.test_agents_run_end_to_end import FakeEngine, engine  # noqa: F401

#: The TUI's A2A read timeout. A turn must not hold the request past it.
CLIENT_READ_TIMEOUT_S = 30.0


def _served_app(monkeypatch: pytest.MonkeyPatch, run_seconds: float) -> Any:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    import arbora.a2a.mcp as a2a_mcp
    from arbora.security import request_identity
    from arbora.security.actor_context import ActorContext, ActorType

    monkeypatch.setitem(
        runtime_config._overrides, "auth_jwt_audience", "arbora-fixture"
    )
    monkeypatch.setitem(
        runtime_config._overrides, "kg_policy_version", "policy:fixture"
    )

    async def credential_owner(credential: str) -> ActorContext:
        assert credential == "fixture-verified"  # sanitizer:ignore - synthetic
        return ActorContext(
            actor_id="person:one",
            actor_type=ActorType.HUMAN,
            tenant_id="tenant:one",
            authenticated=True,
            roles=("kg:read", "kg:write"),
            credential_expires_at=int(time.time()) + 3600,
        )

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)

    async def slow_model(messages: Any, info: Any) -> ModelResponse:
        await asyncio.sleep(run_seconds)
        return ModelResponse(parts=[TextPart("long turn done")])

    host_ports.register_model_provider(lambda _id: FunctionModel(slow_model))
    monkeypatch.setattr(a2a_mcp, "_SERVICE", None)
    _args, mcp, _middlewares = mcp_runtime._build_server(bootstrap=False)
    return mcp.http_app()


def _headers(key: str = "") -> dict[str, str]:
    headers = {
        "Authorization": "Bearer fixture-verified",
        "X-Request-Id": "request:served",
    }
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _send_body(configuration: dict[str, Any] | None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "message": {
            "role": "user",
            "messageId": "message-long",
            "parts": [{"kind": "text", "text": "a long tool turn"}],
            "metadata": {"agent_name": "helper"},
        }
    }
    if configuration is not None:
        params["configuration"] = configuration
    return {"jsonrpc": "2.0", "id": "send", "method": "message/send", "params": params}


async def _follow(
    client: httpx.AsyncClient, task_id: str, *, deadline_s: float
) -> list[str]:
    states: list[str] = []
    stop = time.monotonic() + deadline_s
    while time.monotonic() < stop:
        response = await client.post(
            "/a2a",
            headers=_headers(),
            json={
                "jsonrpc": "2.0",
                "id": "get",
                "method": "tasks/get",
                "params": {"id": task_id},
            },
            timeout=CLIENT_READ_TIMEOUT_S,
        )
        body = response.json()
        assert "error" not in body, body
        state = body["result"]["status"]["state"]
        states.append(state)
        if state in {"completed", "failed", "canceled"}:
            break
        await asyncio.sleep(1.0)
    return states


@pytest.mark.spec("ARBORA-A2A-R001.6")
def test_non_blocking_send_outlives_the_client_read_timeout(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 31 s turn: ``message/send`` answers at once; ``tasks/get`` follows it."""
    run_seconds = CLIENT_READ_TIMEOUT_S + 1.0
    app = _served_app(monkeypatch, run_seconds)

    async def scenario() -> tuple[float, dict[str, Any], list[str], float]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            started = time.monotonic()
            response = await asyncio.wait_for(
                client.post(
                    "/a2a",
                    headers=_headers("long-run-1"),
                    json=_send_body({"blocking": False}),
                ),
                timeout=CLIENT_READ_TIMEOUT_S,
            )
            answered = time.monotonic() - started
            body = response.json()
            assert "error" not in body, body
            states = await _follow(
                client, body["result"]["id"], deadline_s=run_seconds + 30.0
            )
            return answered, body["result"], states, time.monotonic() - started

    answered, task, states, total = asyncio.run(scenario())

    assert answered < 5.0, answered
    assert task["status"]["state"] in {"submitted", "working"}
    assert states[-1] == "completed", states
    assert {"submitted", "working"} & set(states), states
    assert total >= run_seconds
    assert engine.work_items.outcomes == ["succeeded"]


@pytest.mark.spec("ARBORA-A2A-R001.6")
@pytest.mark.parametrize("configuration", [None, {}, {"blocking": True}])
def test_send_without_blocking_false_keeps_blocking(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    configuration: dict[str, Any] | None,
) -> None:
    app = _served_app(monkeypatch, 0.0)

    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            response = await client.post(
                "/a2a",
                headers=_headers("blocking-1"),
                json=_send_body(configuration),
            )
            return response.json()

    body = asyncio.run(scenario())

    assert "error" not in body, body
    assert body["result"]["status"]["state"] == "completed"
    assert engine.work_items.outcomes == ["succeeded"]


@pytest.mark.spec("ARBORA-A2A-R001.6")
def test_unknown_send_configuration_is_refused_before_admission(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _served_app(monkeypatch, 0.0)

    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            response = await client.post(
                "/a2a",
                headers=_headers("bad-config-1"),
                json=_send_body({"blocking": False, "pushNotificationConfig": {}}),
            )
            return response.json()

    body = asyncio.run(scenario())

    assert "error" in body, body
    assert engine.work_items.rows == {}
