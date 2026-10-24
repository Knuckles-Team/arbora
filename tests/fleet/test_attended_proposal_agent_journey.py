"""Real served startup/run-loop/journal acceptance with synthetic model/EG/browser.

Requires the locked test+WebUI extras, no external service or credentials. The
browser transport is an in-process attended responder, not a rendered browser;
this does not prove authenticated WebUI HTTP/WebSocket admission or live EG
storage. Actual FastMCP/SDK lifespans, native tools and consent checks are used.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from agent_utilities import host_ports
from agent_utilities.graph import default_run_plan
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from arbora import host_bindings
from arbora.a2a import mcp as a2a_mcp
from arbora.agent_runs import AgentRunPort
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
from arbora.api.contracts.session import use_session
from arbora.browser_control import browser_control_runtime
from arbora.browser_control.browser_control_client import (
    ControlConfirmMessage,
    ControlResultMessage,
)
from arbora.browser_control.browser_control_runtime import dispatch_browser_operation
from arbora.core.settings import runtime_config
from arbora.fleet import shared_multiplexer
from arbora.fleet.catalog_reader import DeferredFleetCatalogReader
from arbora.gateway import ports
from arbora.mcp_server import runtime
from arbora.mcp_server.composition import install_gateway_application
from arbora.mcp_server.harness_host import (
    register_harness_host,
    unregister_harness_host,
)
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.mcp_server.server import _attach_fleet_runtime
from arbora.security import request_identity
from arbora.security.actor_context import use_actor
from arbora.webui_host.mcp_delegation import webui_mcp_delegation_helpers
from tests.browser_control.test_browser_control_service import _setup
from tests.browser_control.test_browser_proposals import (
    _registered,
    _request,
    _response,
)
from tests.fleet.test_attended_proposal_effect_owner import EffectClient
from tests.test_served_operation_api import FakePool

pytestmark = [pytest.mark.asyncio, pytest.mark.spec("ARBORA-FLEET-R015")]


def _isolate_registration(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    # Public registration APIs mutate these process bindings. Snapshot them so
    # the production startup/shutdown exercised below restores prior test state.
    for name in (
        "_NATIVE",
        "_FLEET",
        "_KEEPALIVE",
        "_REMOTE",
        "_TOOL_CLIENT",
        "_DECIDE",
        "_MODELS",
        "_EVENTS",
    ):
        monkeypatch.setattr(host_ports, name, getattr(host_ports, name))
    monkeypatch.setattr(runtime, "REGISTERED_TOOLS", {})
    monkeypatch.setattr(runtime, "NATIVE_INTENT_TOOLS", {})
    monkeypatch.setattr(runtime, "_FULL_TOOL_NAMES", runtime._FULL_TOOL_NAMES)
    monkeypatch.setattr(runtime, "_get_engine", lambda: client)
    monkeypatch.setattr(shared_multiplexer, "_served", None)
    monkeypatch.setattr(browser_control_runtime, "_service", None)
    monkeypatch.setattr(browser_control_runtime, "_owner_loop", None)
    monkeypatch.setattr(host_bindings, "_agent", None)
    monkeypatch.setattr(a2a_mcp, "_SERVICE", None)
    monkeypatch.setattr(ports, "_application", ports._application)
    monkeypatch.setenv("MCP_FLEET_REGISTRATION", "false")


def _session(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "test-1")
    return request_identity.mint_graph_session(
        request_identity.actor_from_claims(
            {
                "sub": "browser-user",
                "email": "synthetic@example.invalid",
                "tenant_id": "tenant:one",
                "scope": "kg:read kg:write mcp:discover mcp:delegate",
                "roles": ["user"],
                "exp": int(time.time()) + 600,
            }
        )
    )


@asynccontextmanager
async def _served(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Any, list[Any]]]:
    session, client = _session(monkeypatch), EffectClient()
    _isolate_registration(monkeypatch, client)
    invocation = compose_served_invocation(
        pool=FakePool(client),
        graph=session.graph,
        process_session=lambda: session,
        bindings={"browser_control": dispatch_browser_operation},
        fleet_effect=refused_fleet_effect,
    )
    install_gateway_application(invocation)
    _args, mcp, middlewares = runtime._build_server(
        bootstrap=False, invocation=invocation
    )
    for middleware in middlewares:
        mcp.add_middleware(middleware)
    mux = _attach_fleet_runtime(mcp, DeferredFleetCatalogReader())
    register_harness_host(mux)
    events: list[Any] = []
    host_ports.register_run_decision_provider(
        lambda request: default_run_plan(request, {"agent_model": "synthetic"})
    )
    host_ports.register_run_event_sink(events.append)
    try:
        async with mcp._lifespan_manager():
            with use_actor(session.actor), use_session(session):
                yield session, events
    finally:
        unregister_harness_host()
        await mux.aclose()


@dataclass
class _Model:
    payload: dict[str, Any]
    key: str
    repeat: bool
    observed: list[Any] = field(default_factory=list)

    def __call__(self, messages: Any, info: Any) -> ModelResponse:
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        self.observed.extend(part.content for part in returns)
        if not returns or (
            self.repeat and len(returns) == 1 and returns[-1].content.get("ok") is True
        ):
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "act",
                        {
                            "op": "browser.request_call",
                            "params": {"payload": self.payload},
                            "idempotency_key": self.key,
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("proposal finished")])


async def _run(session: Any, run_id: str) -> Any:
    return await AgentRunPort().execute_agent(
        AgentExecutionRequest(
            agent_name="webui-assistant",
            task="request synthetic attended browser action",
            run_id=run_id,
            allowed_tools=host_bindings.preloaded_agent_tools("webui-assistant"),
        ),
        session=session,
    )


async def _message(sent: list[Any], kind: str, run: asyncio.Task[Any]) -> Any:
    async with asyncio.timeout(20):
        while True:
            message = next((item for item in sent if item.type == kind), None)
            if message is not None:
                return message
            if run.done():
                raise AssertionError(f"agent ended before {kind}: {run.result()}")
            await asyncio.sleep(0.01)


async def _human_accepts(
    connection: Any, sent: list[Any], run: asyncio.Task[Any], mutation: bool
) -> None:
    prompt = await _message(sent, "control.proposal_request", run)
    assert prompt.requester_agent == "webui-assistant"
    assert not any(item.type == "control.call" for item in sent)
    await connection.receive(_response(prompt))
    if mutation:
        confirmation = await _message(sent, "control.confirmation_request", run)
        assert not any(item.type == "control.call" for item in sent)
        await connection.receive(
            ControlConfirmMessage(
                call_id=confirmation.call_id,
                confirmation_digest=confirmation.confirmation_digest,
            )
        )
    call = await _message(sent, "control.call", run)
    await connection.receive(
        ControlResultMessage(
            call_id=call.call_id, status="succeeded", result={"ok": True}
        )
    )
    assert (await asyncio.wait_for(run, 15)).output == "proposal finished"


async def _assert_replays(
    session: Any, run_id: str, model: _Model, sent: list[Any], cancel: bool
) -> None:
    before = len(sent)
    if cancel:
        await _run(session, run_id)
        assert model.observed[-1]["error"]["code"] == "INDETERMINATE"
    else:
        assert model.observed[-1]["ok"] is True
        assert model.observed[-1] == model.observed[-2]
        assert sum(item.type == "control.call" for item in sent) == 1
    assert len(sent) == before
    await _run(session, "foreign-" + run_id)
    assert model.observed[-1]["error"]["code"] == "INVALID_ARGUMENT"
    assert len(sent) == before


async def _journey(session: Any, label: str, mutation: bool, cancel: bool) -> None:
    service, connection, engine, sent, tool = await _setup(session, mutation=mutation)
    model = _Model(_request(session, tool).model_dump(mode="json"), label, not cancel)
    host_ports.register_model_provider(lambda _id: FunctionModel(model))
    run_id = "journey-" + label
    with _registered(service):
        task = asyncio.create_task(_run(session, run_id))
        try:
            if cancel:
                await _message(sent, "control.proposal_request", task)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not any(item.type == "control.call" for item in sent)
            else:
                await _human_accepts(connection, sent, task, mutation)
            await _assert_replays(session, run_id, model, sent, cancel)
            assert engine._work_item_engine.claim_count == (0 if cancel else 1)
            assert sent[-1].type == "control.proposal_withdrawn"
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await connection.disconnect("test_finished")


async def test_actual_agent_journal_consent_replay_cancel_and_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as scoped:
        async with _served(scoped) as (session, events):
            helpers = webui_mcp_delegation_helpers()
            inventory = await helpers["list_mcp_server_tools"](server_name="arbora")
            assert {"find", "act"} <= {item["name"] for item in inventory}
            refused = await helpers["call_mcp_tool"](
                server_name="arbora",
                tool_name="act",
                arguments={"op": "browser.execute_call", "params": {"payload": {}}},
            )
            assert refused["error"]["code"] == "SURFACE_NOT_ALLOWED"
            for label, mutation, cancel in (
                ("read", False, False),
                ("mutation", True, False),
                ("cancelled", False, True),
            ):
                await _journey(session, label, mutation, cancel)
            assert any(event["type"] == "tool_call" for event in events)
