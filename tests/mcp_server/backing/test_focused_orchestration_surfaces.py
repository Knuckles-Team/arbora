"""Strict-current ownership contract for the focused Graph-OS execution tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import pytest

from graph_os.mcp_server import runtime
from graph_os.mcp_server.backing.tools import (
    register_analysis_tools,
    register_domain_ops_tools,
    register_governance_tools,
)


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *, name: str, **_metadata):
        def _capture(function):
            self.tools[name] = function
            return function

        return _capture


def _register_all() -> _FakeMCP:
    mcp = _FakeMCP()
    for registrar in (
        register_analysis_tools,
        register_domain_ops_tools,
        register_governance_tools,
    ):
        registrar(mcp)
    return mcp


class _StubBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params):
        self.calls.append((query, params))


class _StubEngine:
    def __init__(self) -> None:
        self.nodes: dict[str, tuple[str, dict]] = {}
        self.backend = _StubBackend()

    def add_node(self, node_id, node_type, props):
        self.nodes[node_id] = (node_type, props)


def _install_reasoning_engine(monkeypatch: pytest.MonkeyPatch) -> _StubEngine:
    engine = _StubEngine()
    monkeypatch.setattr(runtime, "_get_engine", lambda: engine)
    return engine


def _patch_reasoning_agent(monkeypatch: pytest.MonkeyPatch, agent: Any) -> None:
    monkeypatch.setattr(
        "agent_utilities.core.model_factory.create_model", lambda **_kw: object()
    )
    monkeypatch.setattr(
        "agent_utilities.core.contextual_model.create_context_agent",
        lambda **_kw: agent,
    )


async def _reason(topology: str, **extra: Any) -> dict[str, Any]:
    tool = _register_all().tools["graph_agents"]
    return json.loads(
        await tool(
            action="reason",
            task="What is 6*7?",
            context="",
            context_ref="",
            max_fan_out=5,
            max_steps=10,
            host="",
            container_id="",
            options_json="{}",
            topology=topology,
            num_samples=3,
            **extra,
        )
    )


def _dynamic_workflow_tool(
    monkeypatch: pytest.MonkeyPatch, result: Callable[[dict[str, Any]], Any]
) -> tuple[Any, list[dict[str, object]]]:
    """``graph_workflows`` with a recording ``execute_dynamic_workflow``."""
    from agent_utilities.orchestration.manager import Orchestrator

    from graph_os.mcp_server.backing.tools import workflow_tools

    calls: list[dict[str, object]] = []

    async def execute_dynamic(_self, workflow_id: str, task: str, **kwargs):
        calls.append({"workflow_id": workflow_id, "task": task, **kwargs})
        return result(kwargs)

    monkeypatch.setattr(runtime, "_get_engine", lambda: object())
    monkeypatch.setattr(
        workflow_tools,
        "_workflow_gate",
        lambda _engine, _name: {"allowed": True},
    )
    monkeypatch.setattr(Orchestrator, "execute_dynamic_workflow", execute_dynamic)
    return _register_all().tools["graph_workflows"], calls


@pytest.mark.asyncio
async def test_execute_dynamic_live_route_reaches_governed_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool, calls = _dynamic_workflow_tool(
        monkeypatch,
        lambda kw: {
            "backend": "pydantic-ai-harness.dynamic_workflow.DynamicWorkflow",
            "workflow_run_id": "run:00000000000000000000000000000000",
        },
    )

    payload = json.loads(
        await tool(
            action="execute_dynamic",
            workflow="review",
            task="review the change",
            name="",
            export_format="json",
            max_steps=12,
            limit=50,
            max_agent_calls=6,
            max_concurrency=3,
            budget_tokens=4000,
            model_class="economy",
            dynamic_fallback="error",
            workflow_run_id="",
        )
    )

    assert payload["result"]["backend"].endswith("DynamicWorkflow")
    assert calls == [
        {
            "workflow_id": "review",
            "task": "review the change",
            "max_steps": 12,
            "max_agent_calls": 6,
            "max_concurrency": 3,
            "budget_tokens": 4000,
            "model_class": "economy",
            "unavailable_fallback": "error",
            # No resume requested -> a brand-new run, explicitly.
            "workflow_run_id": None,
        }
    ]


def test_budget_domain_action_uses_composed_engine_capability(monkeypatch) -> None:
    from agent_utilities.knowledge_graph.core.engine import IntelligenceGraphEngine
    from agent_utilities.knowledge_graph.orchestration.engine_enterprise import (
        EnterpriseEngineMixin,
    )

    class _RecordingGraph:
        def __init__(self) -> None:
            self.nodes: dict[str, dict[str, object]] = {}

        def add_node(self, node_id: str, **properties: object) -> None:
            self.nodes[node_id] = properties

    assert issubclass(IntelligenceGraphEngine, EnterpriseEngineMixin)
    engine = cast(Any, object.__new__(IntelligenceGraphEngine))
    engine.graph = _RecordingGraph()
    engine.backend = None
    monkeypatch.setattr(runtime, "_get_engine", lambda: engine)

    tool = _register_all().tools["graph_domain_ops"]
    result = json.loads(
        tool(
            action="allocate_budget",
            target_id="business-unit:test",
            amount=2500.0,
            currency="USD",
        )
    )

    assert result["business_unit_id"] == "business-unit:test"
    assert result["amount"] == 2500.0
    assert result["budget_id"] in engine.graph.nodes
