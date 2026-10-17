"""Canonical WorkItem authority and fail-closed routing tests."""

from __future__ import annotations

import json

import pytest

from arbora.a2a.application import _application_error
from arbora.a2a.authority import (
    A2ATransitionHistoryUnavailable,
    WorkItemA2AAuthority,
)
from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
from arbora.a2a.routing import A2AAssemblyUnavailable, OrchestratorA2ARouter


def _message(text: str = "do work") -> A2AMessage:
    return A2AMessage(
        role="user",
        parts=[A2ATextPart(text=text)],
        message_id="message-1",
    )


@pytest.mark.spec("ARBORA-A2A-R002.1")
@pytest.mark.asyncio
async def test_selected_tool_subset_is_refused_before_durable_admission() -> None:
    authority = WorkItemA2AAuthority(lambda: pytest.fail("engine was accessed"))
    with pytest.raises(A2AAssemblyUnavailable, match="cannot yet enforce"):
        await authority.dispatch(
            message=_message(),
            idempotency_key="key",
            decision=A2ARouteDecision(
                agent_name="expert",
                selected_tools=("graph_query",),
                selection_mode="agent-assemble",
            ),
        )


@pytest.mark.asyncio
async def test_context_budget_fails_closed_while_agent_assemble_is_unavailable() -> (
    None
):
    router = OrchestratorA2ARouter(lambda: pytest.fail("engine was accessed"))
    with pytest.raises(A2AAssemblyUnavailable, match="AgentAssemble"):
        await router.route(_message(), context_budget_tokens=4096)


@pytest.mark.spec("ARBORA-A2A-R012.2")
def test_transition_history_unavailable_has_distinct_privacy_safe_error() -> None:
    response = _application_error("rid", A2ATransitionHistoryUnavailable())
    assert response.status_code == 501
    assert json.loads(bytes(response.body)) == {
        "jsonrpc": "2.0",
        "id": "rid",
        "error": {
            "code": -32012,
            "message": "durable task transition history is not available",
        },
    }
    assert "result" not in json.loads(bytes(response.body))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [({"agent_name": "expert"}, "expert"), ({}, None), ({"agent_name": " "}, None)],
)
async def test_router_routes_only_to_the_named_agent(metadata, expected) -> None:
    router = OrchestratorA2ARouter(lambda: pytest.fail("engine was accessed"))
    message = _message().model_copy(update={"metadata": metadata})
    if expected is None:
        with pytest.raises(A2AAssemblyUnavailable, match="agent_name"):
            await router.route(message, context_budget_tokens=None)
        return
    decision = await router.route(message, context_budget_tokens=None)
    assert decision.agent_name == expected
