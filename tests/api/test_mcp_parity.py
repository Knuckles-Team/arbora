"""HTTP and MCP serve one catalog and one pipeline (ARBORA-HOST-R023.2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from arbora.api.http.app import create_api_application
from arbora.api.http.auth import AmbientHTTPAuthenticator
from arbora.api.invoke.pipeline import invoke
from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
from arbora.api.registry import Registry, Verb
from arbora.mcp_server.serving import (
    PARITY_SCHEMA_DIVERGED,
    PARITY_TOOL_MISSING,
    ParityRefused,
    compare_catalogs,
    http_catalog,
    mcp_catalog,
)
from arbora.observability.state import SubsystemWindows
from tests.api.test_invoke_contract import setup_services
from tests.api.test_mcp_serving import _caller, _operation

TOOLS = tuple(f"example.tool{i}" for i in range(8))
SCOPED = frozenset({"example:ask", "mcp:discover"})
UNSCOPED = frozenset({"mcp:discover"})


def _served(scopes: frozenset[str]) -> tuple[Any, Any, Any]:
    ops = [_operation(id=name, verb=Verb.ASK) for name in TOOLS]
    services, runtime, *_ = setup_services(ops[0], registry=Registry(ops))
    caller = _caller(effective_scopes=scopes)
    auth = AmbientHTTPAuthenticator(session_for_request=lambda _: caller.session)

    async def authenticate(request: Any) -> Any:
        return caller

    async def visible(operation: Any, verified: Any) -> bool:
        return True

    auth.authenticate = authenticate
    app = create_api_application(
        services=services, authenticator=auth, invoke=invoke, visibility=visible
    )
    projection = MCPProjection(
        services.registry,
        services,
        SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
        lambda: caller,
        services.policy_gate,
        invoke,
        windows=SubsystemWindows(),
    )
    return app, projection, runtime


@pytest.mark.spec("ARBORA-HOST-R023.2")
async def test_transport_parity_and_unauthorized_refusal() -> None:
    app, projection, runtime = _served(SCOPED)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        document = (await client.get("/registry")).json()
        found = await dispatch_verb("find", projection)
        http, mcp = http_catalog(document), mcp_catalog(found["result"]["items"])
        assert sorted(http) == sorted(mcp) == sorted(TOOLS)
        compare_catalogs(http, mcp)

        over_http = await client.post(f"/ops/{TOOLS[0]}", json={"value": 3})
        over_mcp = await dispatch_verb(
            "ask", projection, op=TOOLS[1], params={"value": 3}
        )
        assert over_http.status_code == 200 and over_mcp["ok"] is True
        assert [call[0].id for call in runtime.calls] == [TOOLS[0], TOOLS[1]]

        denied_app, denied_projection, denied_runtime = _served(UNSCOPED)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=denied_app), base_url="http://fixture"
        ) as other:
            refused_http = await other.post(f"/ops/{TOOLS[0]}", json={"value": 3})
        refused_mcp = await dispatch_verb(
            "ask", denied_projection, op=TOOLS[0], params={"value": 3}
        )
    assert refused_http.status_code >= 400 and refused_mcp["ok"] is False
    assert denied_runtime.calls == []


@pytest.mark.spec("ARBORA-HOST-R023.2")
def test_a_missing_or_diverged_tool_is_refused_with_its_code() -> None:
    both = {"a": '{"x":1}', "b": '{"y":2}'}
    compare_catalogs(both, dict(both))
    with pytest.raises(ParityRefused) as missing:
        compare_catalogs(both, {"a": both["a"]})
    assert (missing.value.code, missing.value.tool) == (PARITY_TOOL_MISSING, "b")
    with pytest.raises(ParityRefused) as diverged:
        compare_catalogs(both, {**both, "b": '{"y":3}'})
    assert (diverged.value.code, diverged.value.tool) == (PARITY_SCHEMA_DIVERGED, "b")
