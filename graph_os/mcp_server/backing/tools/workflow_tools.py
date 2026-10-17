"""Focused graph-os workflow lifecycle operations."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from pydantic import Field

from graph_os.core.handoff import raise_moved
from graph_os.mcp_server import runtime

_WORKFLOW_TASKS: dict[str, asyncio.Task[Any]] = {}


def _workflow_gate(engine: Any, name: str) -> dict[str, Any]:
    from agent_utilities.knowledge_graph.core.workflow_gate import (
        gate_workflow_execution,
    )

    return gate_workflow_execution(engine, name)


def _gate_denial(name: str, gate: dict[str, Any]) -> str:
    return json.dumps(
        {
            "error": "workflow definition failed ontology validation — execution refused",
            "workflow": name,
            "workflow_id": gate.get("workflow_id"),
            "violations": gate.get("violations", []),
        },
        default=str,
    )


def _workflow_mermaid(engine: Any, name: str) -> str | None:
    """Read the persisted topology diagram without failing the workflow operation."""
    try:
        from agent_utilities.knowledge_graph.workflow_store import WorkflowStore

        return WorkflowStore(engine).get_mermaid(name)
    except Exception:
        return None


async def _compile_workflow(
    orchestrator: Any, engine: Any, task: str, name: str
) -> str:
    compiled_name = name or f"compiled_{uuid.uuid4().hex}"
    workflow_id = await orchestrator.compile_workflow(name=compiled_name, task=task)
    return json.dumps(
        {
            "status": "compiled",
            "workflow_id": workflow_id,
            "name": compiled_name,
            "mermaid": await asyncio.to_thread(
                _workflow_mermaid, engine, compiled_name
            ),
        },
        default=str,
    )


async def _compile_process_workflow(engine: Any, workflow: str, name: str) -> str:
    if not workflow:
        raise ValueError("workflow must contain the BusinessProcess id")
    from agent_utilities.knowledge_graph.process_plan_compiler import (
        ProcessPlanCompiler,
    )

    report = await ProcessPlanCompiler(engine).compile_and_store(
        workflow, name=name or None
    )
    report["status"] = "compiled"
    report["mermaid"] = await asyncio.to_thread(
        _workflow_mermaid, engine, report["name"]
    )
    return json.dumps(report, default=str)


async def _list_workflows(engine: Any, limit: int) -> str:
    from agent_utilities.knowledge_graph.workflow_store import WorkflowStore

    workflows = await asyncio.to_thread(WorkflowStore(engine).list_workflows, limit)
    return json.dumps(
        {
            "source": "kg",
            "workflows": workflows,
        },
        default=str,
    )


async def _execute_or_dispatch_workflow(
    action: str,
    orchestrator: Any,
    engine: Any,
    workflow: str,
    task: str,
    max_steps: int,
    max_agent_calls: int,
    max_concurrency: int,
    budget_tokens: int | None,
    model_class: str,
    dynamic_fallback: str,
    workflow_run_id: str,
) -> str:
    if action in {"execute", "execute_dynamic", "dispatch"}:
        if not workflow:
            raise ValueError("workflow is required")
        gate = await asyncio.to_thread(_workflow_gate, engine, workflow)
        if gate.get("allowed") is not True:
            return _gate_denial(workflow, gate)

        if action == "execute":
            return await _execute_workflow(
                orchestrator, engine, workflow, task, max_steps
            )

        if action == "execute_dynamic":
            dynamic_result = await orchestrator.execute_dynamic_workflow(
                workflow_id=workflow,
                task=task,
                max_steps=max_steps,
                max_agent_calls=max_agent_calls,
                max_concurrency=max_concurrency,
                budget_tokens=budget_tokens,
                model_class=model_class,
                unavailable_fallback=dynamic_fallback,
                workflow_run_id=workflow_run_id or None,
            )
            return await _with_mermaid(dynamic_result, engine, workflow)

        return await _dispatch_workflow(engine, workflow, task)

    raise ValueError(f"unsupported workflow execution action: {action}")


async def _execute_workflow(
    orchestrator: Any, engine: Any, workflow: str, task: str, max_steps: int
) -> str:
    result = await orchestrator.execute_workflow(
        workflow_id=workflow,
        task=task,
        max_steps=max_steps,
    )
    return await _with_mermaid(result, engine, workflow)


async def _with_mermaid(result: Any, engine: Any, workflow: str) -> str:
    """``result`` with the workflow's Mermaid diagram, as JSON."""
    return json.dumps(
        {
            "result": result,
            "mermaid": await asyncio.to_thread(_workflow_mermaid, engine, workflow),
        },
        default=str,
    )


async def _dispatch_workflow(engine: Any, workflow: str, task: str) -> str:
    raise_moved("_dispatch_workflow", "workflows.runner", "epistemic-graph")


def _workflow_status(workflow: str) -> str:
    raise_moved("_workflow_status", "workflows.runner", "epistemic-graph")


async def _export_workflow(engine: Any, workflow: str, export_format: str) -> str:
    if not workflow:
        raise ValueError("workflow is required for export")
    from agent_utilities.knowledge_graph.governance_import import export_workflow

    exported = await asyncio.to_thread(
        export_workflow, engine, workflow, fmt=export_format
    )
    return json.dumps(
        exported,
        indent=2,
        default=str,
    )


def register_workflow_tools(mcp: Any) -> None:
    """Register compile, execute, dispatch, inspect, and export operations."""

    @mcp.tool(
        name="graph_workflows",
        description=(
            "Manage governed WorkflowDefinitions. Actions: 'compile', 'compile_process', "
            "'list', 'execute', 'execute_dynamic', 'dispatch', 'status', and 'export'. "
            "execute_dynamic uses upstream Pydantic AI Harness DynamicWorkflow while every "
            "catalog call re-enters GraphOS governance. Execute, execute_dynamic, and "
            "dispatch apply the same ontology/ACL gate; dispatch returns the exact runner "
            "session id."
        ),
        tags=["graph-os", "workflow", "orchestration"],
    )
    async def graph_workflows(
        action: str = Field(
            default="list",
            description=(
                "compile | compile_process | list | execute | execute_dynamic | "
                "dispatch | status | export"
            ),
        ),
        workflow: str = Field(
            default="", description="Workflow name, process id, or run/session id."
        ),
        task: str = Field(
            default="", description="Compilation description or workflow input task."
        ),
        name: str = Field(default="", description="Optional compiled workflow name."),
        export_format: str = Field(
            default="bpmn", description="bpmn | json | skill (export)."
        ),
        max_steps: int = Field(default=30, ge=1),
        limit: int = Field(default=50, ge=1),
        max_agent_calls: int = Field(default=50, ge=1, le=300),
        max_concurrency: int = Field(default=8, ge=1, le=64),
        budget_tokens: int | None = Field(default=None, ge=1),
        model_class: str = Field(default="standard", description="economy | standard"),
        dynamic_fallback: str = Field(
            default="error",
            description=(
                "execute_dynamic only: error | stored_dag. Fallback applies only "
                "when the optional upstream runtime is unavailable."
            ),
        ),
        workflow_run_id: str = Field(
            default="",
            description=(
                "execute_dynamic only: RESUME a halted run. Pass the "
                "workflow_run_id a previous execute_dynamic reported to replay "
                "its already-completed steps instead of re-dispatching them "
                "(the response reports 'resumed' and 'replayed_step_ids'). "
                "Leave empty to start a brand-new run."
            ),
        ),
    ) -> str:
        raise_moved("graph_workflows", "orchestration.manager", "epistemic-graph")

    runtime.REGISTERED_TOOLS["graph_workflows"] = graph_workflows
    runtime.ACTION_TOOL_ROUTES["graph_workflows"] = "/graph/workflows"
