"""Strict-current ownership contract for the focused Graph-OS execution tools."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from typing import Any

import pytest

from graph_os.mcp_server import runtime
from graph_os.mcp_server.backing.tools import (
    register_agent_execution_tools,
    register_analysis_tools,
    register_domain_ops_tools,
    register_evolution_tools,
    register_governance_tools,
    register_job_tools,
    register_workflow_tools,
)
from scripts.gen_graphos_manifest import harvest_actions


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
        register_agent_execution_tools,
        register_domain_ops_tools,
        register_evolution_tools,
        register_governance_tools,
        register_job_tools,
        register_workflow_tools,
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


def test_orchestration_capabilities_have_one_current_owner() -> None:
    mcp = _register_all()

    expected_actions = {
        "graph_agents": {
            "swarm",
            "computer_use",
            "synthesize_org",
            "run_org",
            "reason",
        },
        "graph_domain_ops": {
            "allocate_budget",
            "fit_markov_regime",
            "register_rlm_actor",
        },
        "graph_evolution": {
            "assimilate",
            "audit_scan",
            "distill_skills",
            "standardize",
            "failure_ingest",
            "optimize_component",
            "publish_proposal",
            "evidence_lineage",
        },
        "graph_governance": {
            "claim_ownership",
            "grant_approval",
            "ownership_apply",
            "ownership_report",
            "policy_status",
            "submit_risk_veto",
            "verify_action",
        },
        "graph_jobs": {
            "dispatch",
            "status",
            "cancel",
            "input",
            "drain",
            "dead_letter",
        },
        "graph_workflows": {
            "compile",
            "compile_process",
            "list",
            "execute",
            "execute_dynamic",
            "dispatch",
            "status",
            "export",
        },
    }
    # 37, computed from `expected_actions` above, not guessed. This total has now
    # gone stale four times, each time the same way: a lane adds an action to a
    # tool, updates the action SET here, and does not touch this line -- so git
    # merges the set change cleanly and leaves the count behind.
    #   feat/wave7-followups-evolution -> graph_evolution "evidence_lineage" (D-71-4)
    #   feat/wave25-followups-mcpapps  -> graph_jobs      "input"
    #   83ee1066 governed-promotion    -> graph_jobs      "drain", "dead_letter"
    #   goc/goc-61-admin-claim-ownership -> graph_governance "claim_ownership" (GOC-61)
    # The third did not update the set either, so this test was RED on main until
    # both were added above. The `harvest_actions(...) == actions` assertion below
    # is what actually pins the surface against the real tools; this total is the
    # redundant ratchet that catches a silently added action.
    assert sum(map(len, expected_actions.values())) == 37
    for tool, actions in expected_actions.items():
        assert harvest_actions(mcp.tools[tool]) == actions

    orchestrate = mcp.tools["graph_orchestrate"]
    assert "action" not in inspect.signature(orchestrate).parameters
    assert harvest_actions(orchestrate) == set()


def test_focused_tools_publish_collapsed_rest_routes() -> None:
    _register_all()
    expected_routes = {
        "graph_agents": "/graph/agents",
        "graph_domain_ops": "/graph/domain-ops",
        "graph_evolution": "/graph/evolution",
        "graph_governance": "/graph/governance",
        "graph_jobs": "/graph/jobs",
        "graph_workflows": "/graph/workflows",
    }
    assert {
        tool: runtime.ACTION_TOOL_ROUTES.get(tool) for tool in expected_routes
    } == expected_routes
    assert "graph_rlm" not in runtime.REGISTERED_TOOLS
    assert "/graph/rlm" not in runtime.ACTION_TOOL_ROUTES.values()


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


@pytest.mark.asyncio
async def test_execute_dynamic_forwards_workflow_run_id_so_resume_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the governed resume cache is keyed strictly on
    ``workflow_run_id``, but neither ``graph_workflows`` nor
    ``Orchestrator.execute_dynamic_workflow`` accepted or forwarded one -- so
    every live invocation minted a fresh id and the resume path, though fully
    implemented and unit-tested, was unreachable from the only entry point that
    exists.
    """
    tool, calls = _dynamic_workflow_tool(
        monkeypatch,
        lambda kw: {"backend": "x", "workflow_run_id": kw.get("workflow_run_id")},
    )

    await tool(
        action="execute_dynamic",
        workflow="review",
        task="review the change",
        workflow_run_id="run:deadbeef",
    )
    assert calls[0]["workflow_run_id"] == "run:deadbeef"


@pytest.mark.asyncio
async def test_graph_agents_reason_tot_degrades_honestly_with_no_reachable_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No model reachable -> ToT's generate_fn returns no children, so
    run_tot converges on the ROOT node itself (degraded, never a crash and
    never a fabricated answer) -- mirrors _reasoning_step_fn's same
    no-model-reachable honesty contract for CoT."""
    from graph_os.mcp_server.backing.tools import agent_execution_tools  # noqa: F401

    _install_reasoning_engine(monkeypatch)
    monkeypatch.setattr(
        "agent_utilities.core.model_factory.create_model",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("no model reachable")),
    )

    payload = await _reason("tot_dfs", branching_factor=3, beam_width=3, max_depth=5)

    assert payload["topology"] == "tot_dfs"
    assert payload["node_count"] == 1  # only the root -- no children generated
    assert payload["termination"]["degraded"] is True
