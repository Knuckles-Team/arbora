"""Live-path tests for the G1 gateway to G2 application boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.gateway import ontology_api
from arbora.gateway.state import GatewayState
from arbora.mcp_server.host_caches import build_host_caches


class _Application:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_tool(self, tool: str, /, **kwargs: Any) -> Any:
        self.calls.append((tool, kwargs))
        return {"tool": tool, **kwargs}

    async def invoke_operation(
        self,
        op_id: str,
        params: Mapping[str, Any],
        /,
        *,
        request: Any,
        idempotency_key: str | None = None,
    ) -> Any:
        self.calls.append((op_id, dict(params)))
        return {"op": op_id, "key": idempotency_key}

    def engine(self) -> object:
        return self

    def ensure_tools_registered(self) -> None:
        return None

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, str]:
        return {"approval_id": approval_id, "decision": decision}

    def mount_rest_routes(self, app: Any, *, prefix: str) -> None:
        app.append(prefix)

    def remote_oauth_grant_bindings(self, actor: Any) -> tuple[Any, ...]:
        return ()

    def toggle_states_batch(
        self, engine: Any, items: Sequence[tuple[str, str]]
    ) -> Mapping[tuple[str, str], bool]:
        return dict.fromkeys(items, True)


@pytest.mark.asyncio
async def test_ontology_route_dispatches_through_configured_application_port() -> None:
    from types import SimpleNamespace

    application = _Application()
    gateway = GatewayState()
    gateway.bind_application(application)
    host = SimpleNamespace(gateway=gateway)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host=host)))

    result = await ontology_api._call(
        request, "ontology_value_types", action="describe", name="email"
    )

    assert result == {
        "tool": "ontology_value_types",
        "action": "describe",
        "name": "email",
    }
    assert application.calls == [
        ("ontology_value_types", {"action": "describe", "name": "email"})
    ]
    assert gateway.application() is application


@pytest.mark.spec("ARBORA-OPS-R039.7")
@pytest.mark.asyncio
async def test_invoke_operation_runs_the_invoke_pipeline_as_the_request_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from arbora.api.invoke import pipeline
    from arbora.mcp_server.composition import NativeGatewayApplication
    from arbora.security.operation_result import OpError, OpResult

    caller = object()
    seen: list[Any] = []

    class Authenticator:
        async def authenticate(self, request: Any) -> Any:
            seen.append(request)
            return caller

    answers: list[Any] = [OpResult(value={"ok": 1}), OpError("NOT_FOUND")]

    async def fake_invoke(
        op_id: str, params: Any, who: Any, surface: Any, **kw: Any
    ) -> Any:
        seen.append((op_id, dict(params), who, surface.value, kw["idempotency_key"]))
        return answers.pop(0)

    monkeypatch.setattr(pipeline, "invoke", fake_invoke)
    invocation = SimpleNamespace(authenticator=Authenticator(), services="services")
    application = NativeGatewayApplication(
        invocation,
        fleet=None,
        approvals=None,  # type: ignore[arg-type]
        engine_provider=lambda: None,
        caches=build_host_caches(RuntimeConfig()),
    )
    value = await application.invoke_operation(
        "ingest.sources.list", {}, request="req", idempotency_key="k"
    )
    assert value == {"ok": 1}
    assert seen == ["req", ("ingest.sources.list", {}, caller, "http", "k")]
    with pytest.raises(pipeline.OperationRefused) as refused:
        await application.invoke_operation("ingest.sources.list", {}, request="req")
    assert refused.value.code == "NOT_FOUND"
    bare = NativeGatewayApplication(
        None,
        fleet=None,
        approvals=None,
        engine_provider=lambda: None,
        caches=build_host_caches(RuntimeConfig()),  # type: ignore[arg-type]
    )
    with pytest.raises(pipeline.OperationRefused):
        await bare.invoke_operation("x", {}, request="req")
