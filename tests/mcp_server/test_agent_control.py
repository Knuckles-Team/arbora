"""Typed AU control-plane composition tests."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.contracts.agent_control_plane import GraphRlmRunRequest
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import runtime
from arbora.mcp_server.agent_control import register_graph_rlm
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.process_authority import ProcessAuthority
from arbora.security.session import GraphSession, resolve_session


class Mcp:
    """A fake structurally satisfying ``arbora.mcp_server.agent_control.ToolRegistrar``."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs: Any) -> Callable[[Any], Any]:
        name = kwargs["name"]

        def decorate(function: Any) -> Any:
            self.tools[name] = function
            return function

        return decorate


def _verified_session() -> GraphSession:
    actor = ActorContext(
        actor_id="rlm-user",
        actor_type=ActorType.HUMAN,
        roles=("user",),
        tenant_id="tenant-a",
        authenticated=True,
    )
    return GraphSession(
        actor=actor,
        tenant="tenant-a",
        scopes=frozenset({"kg:read"}),
        graph="tenant-a",
        policy_version="policy-7",
        audience="graph-runtime",
    )


@pytest.mark.asyncio
async def test_graph_rlm_is_composed_from_injected_authorities() -> None:
    mcp = Mcp()
    client = object()
    session = _verified_session()
    calls: list[GraphRlmRunRequest] = []

    class ControlPlane:
        async def graph_rlm(self, request: GraphRlmRunRequest):
            calls.append(request)
            return SimpleNamespace(model_dump_json=lambda: '{"action":"run","ok":true}')

    def factory(
        actual_client: Any, actual_session: Any, *, origins: Any, run_port: Any
    ):
        assert actual_client is client
        assert actual_session is session
        assert resolve_session(actual_session) is session
        return ControlPlane()

    table = ToolTable(ProcessAuthority(config=RuntimeConfig()))
    table.authority.bind_session(session)
    register_graph_rlm(
        mcp,
        table,
        client_for_session=lambda actual_session: (
            client if actual_session is session else None
        ),
        origins=object(),
        run_port=object(),
        factory=factory,
    )
    request = GraphRlmRunRequest(task="inspect", input_text="state")
    assert await mcp.tools["graph_rlm"](request) == ('{"action":"run","ok":true}')
    assert calls == [request]
    assert table["graph_rlm"] is mcp.tools["graph_rlm"]
    assert table.routes["graph_rlm"] == "/graph/rlm"


@pytest.mark.asyncio
async def test_graph_rlm_call_has_no_unverified_fallback() -> None:
    mcp = Mcp()

    @contextlib.contextmanager
    def missing_scope(_authority):
        raise PermissionError("verified authorities required")
        yield

    original_scope = runtime.verified_tool_session_scope
    runtime.verified_tool_session_scope = missing_scope
    try:
        register_graph_rlm(
            mcp,
            ToolTable(ProcessAuthority(config=RuntimeConfig())),
            client_for_session=lambda session: object(),
            origins=object(),
            run_port=object(),
        )
        with pytest.raises(PermissionError, match="verified authorities required"):
            await mcp.tools["graph_rlm"](
                GraphRlmRunRequest(task="inspect", input_text="state")
            )
    finally:
        runtime.verified_tool_session_scope = original_scope


@pytest.mark.asyncio
async def test_graph_rlm_rejects_mismatched_ambient_session() -> None:
    mcp = Mcp()

    @contextlib.contextmanager
    def mismatched_scope(_authority):
        raise PermissionError("Verified actor and GraphSession authority differ")
        yield

    original_scope = runtime.verified_tool_session_scope
    runtime.verified_tool_session_scope = mismatched_scope
    try:
        register_graph_rlm(
            mcp,
            ToolTable(ProcessAuthority(config=RuntimeConfig())),
            client_for_session=lambda session: object(),
            origins=object(),
            run_port=object(),
        )
        with pytest.raises(PermissionError, match="authority differ"):
            await mcp.tools["graph_rlm"](
                GraphRlmRunRequest(task="inspect", input_text="state")
            )
    finally:
        runtime.verified_tool_session_scope = original_scope
