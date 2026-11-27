"""Prompt-free flat execution snapshots, separate from public replay v1."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from graphlib import TopologicalSorter
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from arbora.api.ops._common import Params
from arbora.api.ops.run_replay import MAX_PAGE_BYTES, RunReadParams
from arbora.core.settings import RuntimeConfig

GRAPH_SCHEMA: Literal["arbora-execution-graph-v1"] = "arbora-execution-graph-v1"
MAX_GRAPH_EVENTS = 1000
MAX_GRAPH_NODES = 5000
MAX_GRAPH_EDGES = 20000
PartialReason = Literal["node_bound", "edge_bound", "depth_bound", "child_unavailable"]
StepStatus = Literal[
    "unobserved", "running", "completed", "failed", "cancelled", "blocked"
]
TASK_STATES: Mapping[str, StepStatus] = MappingProxyType(
    {
        "task_started": "running",
        "task_completed": "completed",
        "task_failed": "failed",
        "task_cancelled": "cancelled",
        "task_blocked": "blocked",
    }
)


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


class ExecutionChildRun(Params):
    """A producer-declared nested run; never a conversation or a target."""

    key: str = Field(min_length=1, max_length=16384)
    kind: Literal["child_run"] = "child_run"
    run_id: str = Field(min_length=1, max_length=512)
    task_id: str = Field(min_length=1, max_length=512)
    status: Literal["unobserved"] = "unobserved"
    depth: int = Field(ge=1, le=8, strict=True)


class ExecutionDependency(Params):
    kind: Literal["depends_on"] = "depends_on"
    source: str = Field(min_length=1, max_length=16384)
    target: str = Field(min_length=1, max_length=16384)


class ExecutionSpawn(Params):
    """Containment: the declared spawning step to the declared child run."""

    kind: Literal["spawned_child"] = "spawned_child"
    source: str = Field(min_length=1, max_length=16384)
    target: str = Field(min_length=1, max_length=16384)
    evidence: StepEvidence


class DeclaredSpawn(Params):
    """One ``spawned_child`` event of this run, with its native identity."""

    parent_run_id: str = Field(min_length=1, max_length=512)
    child_run_id: str = Field(min_length=1, max_length=512)
    child_task_id: str = Field(min_length=1, max_length=512)
    task_id: str = Field(min_length=1, max_length=512)
    event_id: str = Field(min_length=1, max_length=1024)
    sequence: int = Field(ge=1, le=2**63 - 1, strict=True)


class RunCursor(Params):
    run_id: str = Field(min_length=1, max_length=512)
    covered_after: int = Field(ge=0, le=MAX_GRAPH_EVENTS, strict=True)


class ExecutionGraphSnapshot(Params):
    plan_digest: str = Field(min_length=1, max_length=512)
    nodes: list[
        Annotated[ExecutionStep | ExecutionChildRun, Field(discriminator="kind")]
    ] = Field(min_length=1, max_length=MAX_GRAPH_NODES)
    edges: list[
        Annotated[ExecutionDependency | ExecutionSpawn, Field(discriminator="kind")]
    ] = Field(max_length=MAX_GRAPH_EDGES)
    topology_completeness: Literal["complete", "partial"] = "complete"
    partial_reason: PartialReason | None = None
    lifecycle_completeness: Literal["incomplete"] = "incomplete"
    lifecycle_reason: Literal["observed_prefix_only"] = "observed_prefix_only"
    covered_after: int = Field(ge=0, le=MAX_GRAPH_EVENTS, strict=True)
    has_more: bool
    spawns: list[DeclaredSpawn] = Field(default_factory=list, max_length=64)
    run_cursors: list[RunCursor] = Field(default_factory=list, max_length=64)
    can_read_transcript: Literal[False] = False
    can_message_active: Literal[False] = False
    can_continue_completed: Literal[False] = False
    supports_spawned_children: bool = False


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
    _validate_predecessors(topology)
    return digest, topology


def _validate_predecessors(topology: dict[str, list[str]]) -> None:
    if any(dep not in topology for deps in topology.values() for dep in deps):
        raise ValueError("unknown predecessor")
    # Native admission owns planning; malformed read projections fail closed.
    tuple(TopologicalSorter(topology).static_order())


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
    *,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reduce only validated replay rows; never derive edges from event names.

    ``base`` is an earlier result of this function for the same run; ``rows``
    are then the ordered updates after its covered cursor, and the result
    equals a fresh projection of the whole history at the new cursor.
    """
    if page.get("availability") != "available":
        raise ValueError("unavailable replay")
    digest, tasks = topology
    nodes, spawns = _initial_state(request, digest, tasks, page, base)
    _reduce_rows(request.run_id, (digest, tasks), rows, page, nodes, spawns)
    snapshot = ExecutionGraphSnapshot(
        plan_digest=digest,
        nodes=list(nodes.values()),
        edges=_bounded_edges(nodes, tasks),
        covered_after=page["next_after"],
        has_more=page["has_more"],
        spawns=spawns,
        run_cursors=[
            RunCursor(run_id=request.run_id, covered_after=page["next_after"])
        ],
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


def _reduce_rows(
    run_id: str,
    topology: tuple[str, dict[str, list[str]]],
    rows: list[dict[str, Any]],
    page: dict[str, Any],
    nodes: dict[str, ExecutionStep],
    spawns: list[DeclaredSpawn],
) -> None:
    digest, tasks = topology
    for row, event in zip(rows, page["events"], strict=False):
        raw = json.loads(row["payload"])
        if event["type"] == "spawned_child":
            spawns.append(_declared_spawn(raw, event, nodes, run_id, spawns))
        else:
            _apply_evidence(raw, event, nodes, digest, tasks, run_id)


def _initial_state(
    request: ExecutionGraphParams,
    digest: str,
    tasks: dict[str, list[str]],
    page: dict[str, Any],
    base: dict[str, Any] | None,
) -> tuple[dict[str, ExecutionStep], list[DeclaredSpawn]]:
    if base is None:
        return _fresh_nodes(request.run_id, tasks), []
    snapshot = _bound_base(request.run_id, digest, page, base)
    own = [
        ExecutionStep.model_validate(node)
        for node in snapshot["nodes"]
        if _is_own_step(node, request.run_id)
    ]
    if {node.task_id for node in own} != set(tasks) or len(own) != len(tasks):
        raise ValueError("base snapshot does not bind this plan")
    spawns = [DeclaredSpawn.model_validate(item) for item in snapshot["spawns"]]
    return {node.task_id: node for node in own}, spawns


def _fresh_nodes(run_id: str, tasks: dict[str, list[str]]) -> dict[str, ExecutionStep]:
    return {
        task_id: ExecutionStep(key=step_key(run_id, task_id), task_id=task_id)
        for task_id in tasks
    }


def _bound_base(
    run_id: str, digest: str, page: dict[str, Any], base: dict[str, Any]
) -> dict[str, Any]:
    snapshot = base.get("snapshot")
    if not isinstance(snapshot, dict) or base.get("run_id") != run_id:
        raise ValueError("base snapshot does not bind this cursor")
    if snapshot.get("plan_digest") != digest:
        raise ValueError("base snapshot does not bind this cursor")
    if snapshot.get("covered_after") != page.get("after"):
        raise ValueError("base snapshot does not bind this cursor")
    return snapshot


def _is_own_step(node: dict[str, Any], run_id: str) -> bool:
    return node.get("kind") == "plan_step" and node.get("key") == step_key(
        run_id, node.get("task_id", "")
    )


def _declared_spawn(
    raw: dict[str, Any],
    event: dict[str, Any],
    nodes: dict[str, ExecutionStep],
    run_id: str,
    seen: list[DeclaredSpawn],
) -> DeclaredSpawn:
    """Only a declared step may spawn; lineage is never inferred."""
    child = _identifier(raw.get("child_run_id"))
    task_id = event["task_id"]
    if (
        raw.get("parent_run_id") != run_id
        or child == run_id
        or task_id not in nodes
        or any(item.child_run_id == child for item in seen)
    ):
        raise ValueError("conflicting lineage")
    return DeclaredSpawn(
        parent_run_id=run_id,
        child_run_id=child,
        child_task_id=_identifier(raw.get("child_task_id", child)),
        task_id=task_id,
        event_id=event["event_id"],
        sequence=event["sequence"],
    )


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
    _validate_transition(node.status, kind)
    node.status = TASK_STATES[kind]
    node.evidence = StepEvidence(event_id=event["event_id"], sequence=event["sequence"])


def _validate_transition(status: StepStatus, kind: str) -> None:
    if status != "unobserved" and (
        status != "running" or kind in {"task_started", "task_blocked"}
    ):
        raise ValueError("conflicting lifecycle transition")
    if kind in {"task_completed", "task_failed", "task_cancelled"} and (
        status != "running"
    ):
        raise ValueError("missing start evidence")


@dataclass(frozen=True, slots=True)
class GraphBounds:
    """``runs_graph_max_*`` of the Arbora configuration, already range-checked."""

    max_nodes: int = 500
    max_edges: int = 2000
    max_depth: int = 4

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> GraphBounds:
        return cls(
            max_nodes=int(config.runs_graph_max_nodes),
            max_edges=int(config.runs_graph_max_edges),
            max_depth=int(config.runs_graph_max_depth),
        )


@dataclass(frozen=True, slots=True)
class ChildPart:
    """One declared child with its own authorized snapshot, or ``None``."""

    spawn: DeclaredSpawn
    depth: int
    snapshot: dict[str, Any] | None


def _cursors_of(part: ChildPart) -> list[Any]:
    child = part.snapshot
    assert child is not None
    return list(child["run_cursors"])


def child_key(run_id: str) -> str:
    return json.dumps([run_id, "child_run", run_id], separators=(",", ":"))


def nest_children(
    snapshot: dict[str, Any],
    parts: list[ChildPart],
    bounds: GraphBounds,
    reason: PartialReason | None = None,
) -> dict[str, Any]:
    """Attach authorized child snapshots; truncation is always reported."""
    nodes, edges = list(snapshot["nodes"]), list(snapshot["edges"])
    cursors = list(snapshot["run_cursors"])
    merged = False
    for part in parts:
        verdict = _admit_part(part, len(nodes), len(edges), bounds)
        if isinstance(verdict, str):
            reason = reason or verdict
            continue
        nodes += verdict[0]
        edges += verdict[1]
        cursors += _cursors_of(part)
        merged = True
        reason = reason or _child_reason(part)
    result = {**snapshot, "nodes": nodes, "edges": edges, "run_cursors": cursors}
    result["supports_spawned_children"] = merged
    if reason is not None:
        result["topology_completeness"] = "partial"
        result["partial_reason"] = reason
    return ExecutionGraphSnapshot.model_validate(result).model_dump(mode="json")


def _child_reason(part: ChildPart) -> PartialReason | None:
    child = part.snapshot or {}
    return child["partial_reason"] if child.get("partial_reason") else None


def _admit_part(
    part: ChildPart, node_count: int, edge_count: int, bounds: GraphBounds
) -> PartialReason | tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The whole child fits, or a reason it was dropped; never half a child."""
    if part.snapshot is None:
        return "child_unavailable"
    added_nodes, added_edges = _child_members(part)
    if node_count + len(added_nodes) > bounds.max_nodes:
        return "node_bound"
    if edge_count + len(added_edges) > bounds.max_edges:
        return "edge_bound"
    return added_nodes, added_edges


def _child_members(
    part: ChildPart,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    spawn, child = part.spawn, part.snapshot
    assert child is not None
    node = ExecutionChildRun(
        key=child_key(spawn.child_run_id),
        run_id=spawn.child_run_id,
        task_id=spawn.child_task_id,
        depth=part.depth,
    )
    edge = ExecutionSpawn(
        source=step_key(spawn.parent_run_id, spawn.task_id),
        target=node.key,
        evidence=StepEvidence(event_id=spawn.event_id, sequence=spawn.sequence),
    )
    return (
        [node.model_dump(mode="json"), *child["nodes"]],
        [edge.model_dump(mode="json"), *child["edges"]],
    )
