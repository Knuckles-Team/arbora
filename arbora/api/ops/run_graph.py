"""Prompt-free flat execution snapshots, separate from public replay v1."""

from __future__ import annotations

import json
from graphlib import TopologicalSorter
from typing import Any, Literal

from pydantic import Field, model_validator

from arbora.api.ops._common import Params
from arbora.api.ops.run_replay import MAX_PAGE_BYTES, RunReadParams

GRAPH_SCHEMA: Literal["arbora-execution-graph-v1"] = "arbora-execution-graph-v1"
MAX_GRAPH_EVENTS = 1000
StepStatus = Literal[
    "unobserved", "running", "completed", "failed", "cancelled", "blocked"
]
TASK_STATES: dict[str, StepStatus] = {
    "task_started": "running",
    "task_completed": "completed",
    "task_failed": "failed",
    "task_cancelled": "cancelled",
    "task_blocked": "blocked",
}


class ExecutionGraphParams(RunReadParams):
    schema_version: Literal["arbora-execution-graph-v1"]


class StepEvidence(Params):
    event_id: str = Field(min_length=1, max_length=1024)
    sequence: int = Field(ge=1, le=2**63 - 1, strict=True)


class ExecutionStep(Params):
    key: str = Field(min_length=1, max_length=16384)
    kind: Literal["plan_step"] = "plan_step"
    task_id: str = Field(min_length=1, max_length=512)
    status: StepStatus = "unobserved"
    evidence: StepEvidence | None = None


class ExecutionDependency(Params):
    kind: Literal["depends_on"] = "depends_on"
    source: str = Field(min_length=1, max_length=16384)
    target: str = Field(min_length=1, max_length=16384)


class ExecutionGraphSnapshot(Params):
    plan_digest: str = Field(min_length=1, max_length=512)
    nodes: list[ExecutionStep] = Field(min_length=1, max_length=64)
    edges: list[ExecutionDependency] = Field(max_length=2016)
    topology_completeness: Literal["complete"] = "complete"
    lifecycle_completeness: Literal["incomplete"] = "incomplete"
    lifecycle_reason: Literal["observed_prefix_only"] = "observed_prefix_only"
    covered_after: int = Field(ge=0, le=MAX_GRAPH_EVENTS, strict=True)
    has_more: bool
    can_read_transcript: Literal[False] = False
    can_message_active: Literal[False] = False
    can_continue_completed: Literal[False] = False
    supports_spawned_children: Literal[False] = False


class ExecutionGraphRead(Params):
    schema_version: Literal["arbora-execution-graph-v1"] = GRAPH_SCHEMA
    availability: Literal["available", "unavailable"]
    run_id: str = Field(min_length=1, max_length=512)
    task_id: str = Field(min_length=1, max_length=512)
    snapshot: ExecutionGraphSnapshot | None = None

    @model_validator(mode="after")
    def availability_matches(self) -> ExecutionGraphRead:
        if (self.availability == "available") != (self.snapshot is not None):
            raise ValueError("graph availability must match snapshot presence")
        return self


class ExecutionGraphResult(Params):
    value: ExecutionGraphRead


def unavailable_graph(request: ExecutionGraphParams) -> dict[str, Any]:
    return ExecutionGraphRead(
        availability="unavailable",
        run_id=request.run_id,
        task_id=request.task_id or request.run_id,
    ).model_dump(mode="json")


def step_key(run_id: str, task_id: str) -> str:
    """Unambiguous stable identity, even when IDs contain delimiter characters."""
    return json.dumps([run_id, "plan_step", task_id], separators=(",", ":"))


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("invalid graph identity")
    return value


def declared_topology(raw: dict[str, Any]) -> tuple[str, dict[str, list[str]]]:
    """Validate projection structure, not plan admission or execution policy."""
    plan = raw.get("task_plan")
    if not isinstance(plan, dict):
        raise ValueError("declared plan unavailable")
    digest = _identifier(plan.get("plan_digest"))
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not 0 < len(tasks) <= 64:
        raise ValueError("declared plan bounds")
    topology: dict[str, list[str]] = {}
    for task in tasks:
        _add_task(topology, task)
    if type(plan.get("total_tasks")) is not int or plan["total_tasks"] != len(topology):
        raise ValueError("incomplete topology")
    if plan.get("output_task_id") not in topology:
        raise ValueError("unknown output task")
    if any(dep not in topology for deps in topology.values() for dep in deps):
        raise ValueError("unknown predecessor")
    # Native admission owns planning; malformed read projections fail closed.
    tuple(TopologicalSorter(topology).static_order())
    return digest, topology


def _add_task(topology: dict[str, list[str]], task: Any) -> None:
    if not isinstance(task, dict):
        raise ValueError("invalid declared task")
    identity = _identifier(task.get("task_id"))
    dependencies = task.get("depends_on")
    if not isinstance(dependencies, list) or len(dependencies) > 64:
        raise ValueError("invalid predecessors")
    dependencies = [_identifier(dep) for dep in dependencies]
    if identity in topology or len(set(dependencies)) != len(dependencies):
        raise ValueError("conflicting task identity")
    topology[identity] = dependencies


def project_graph(
    request: ExecutionGraphParams,
    topology: tuple[str, dict[str, list[str]]],
    rows: list[dict[str, Any]],
    page: dict[str, Any],
) -> dict[str, Any]:
    """Reduce only validated replay rows; never derive edges from event names."""
    if page.get("availability") != "available":
        raise ValueError("unavailable replay")
    digest, tasks = topology
    nodes = {
        task_id: ExecutionStep(key=step_key(request.run_id, task_id), task_id=task_id)
        for task_id in tasks
    }
    for row, event in zip(rows, page["events"], strict=False):
        raw = json.loads(row["payload"])
        _apply_evidence(raw, event, nodes, digest, tasks, request.run_id)
    snapshot = ExecutionGraphSnapshot(
        plan_digest=digest,
        nodes=list(nodes.values()),
        edges=_bounded_edges(nodes, tasks),
        covered_after=page["next_after"],
        has_more=page["has_more"],
    )
    result = ExecutionGraphRead(
        availability="available",
        run_id=request.run_id,
        task_id=request.task_id or request.run_id,
        snapshot=snapshot,
    ).model_dump(mode="json")
    if len(json.dumps(result).encode()) > MAX_PAGE_BYTES:
        raise ValueError("execution graph exceeds byte bound")
    return result


def _bounded_edges(
    nodes: dict[str, ExecutionStep], tasks: dict[str, list[str]]
) -> list[ExecutionDependency]:
    """Bound contributions before allocating a whole serialized dense graph."""
    size = 0
    for node in nodes.values():
        size = _bounded_contribution(node, size)
    edges = []
    for task_id, dependencies in tasks.items():
        for dependency in dependencies:
            edge = ExecutionDependency(
                source=nodes[dependency].key, target=nodes[task_id].key
            )
            size = _bounded_contribution(edge, size)
            edges.append(edge)
    return edges


def _bounded_contribution(value: Params, size: int) -> int:
    # Summed members are a lower bound; the final exact check includes framing.
    size += len(json.dumps(value.model_dump(mode="json")).encode())
    if size > MAX_PAGE_BYTES:
        raise ValueError("execution graph exceeds byte bound")
    return size


def _apply_evidence(
    raw: dict[str, Any],
    event: dict[str, Any],
    nodes: dict[str, ExecutionStep],
    digest: str,
    tasks: dict[str, list[str]],
    run_id: str,
) -> None:
    kind = event["type"]
    if kind not in TASK_STATES:
        return
    task_id = event["task_id"]
    if (
        task_id not in nodes
        or raw.get("plan_id") != digest
        or raw.get("parent_run_id") != run_id
        or raw.get("depends_on") != tasks[task_id]
        or raw.get("status") != TASK_STATES[kind]
    ):
        raise ValueError("conflicting lifecycle binding")
    node = nodes[task_id]
    if node.status != "unobserved" and (
        node.status != "running" or kind in {"task_started", "task_blocked"}
    ):
        raise ValueError("conflicting lifecycle transition")
    if kind in {"task_completed", "task_failed", "task_cancelled"} and (
        node.status != "running"
    ):
        raise ValueError("missing start evidence")
    node.status = TASK_STATES[kind]
    node.evidence = StepEvidence(event_id=event["event_id"], sequence=event["sequence"])
