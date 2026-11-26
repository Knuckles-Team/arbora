"""Served harness component and improvement library operations (ARBORA-OPS-R068).

Reads go through the engine as the verified caller. Components are read with the
engine ``Uql`` method over ``HarnessComponent`` objects. Lessons and the adoption
use the ``DecisionLog`` ops ``harness_lessons`` and ``harness_adoption``. Arbora
ranks nothing and stores nothing: the engine orders lessons by confidence and
writes the adopted harness. A foreign or absent object answers the same refusal.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from medulla.generated.models import HarnessAdoptionWrite
from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.ops.ontology_objects import _Statement
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Executor,
    Idempotency,
    OpSpec,
    PrincipalRule,
    SubjectRef,
    SubjectSource,
    Verb,
)

READ_SCOPE = "kg:read"
ADOPT_SCOPE = "decide:commit"
#: Replay owner of the adoption: the engine decision log replays a write by key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "harness.evolution.adopt": "medulla:DecisionLog",
}
_KINDS = (
    "system_prompt",
    "memory",
    "tool_guidance",
    "subagent",
    "skill",
    "script",
    "plugin",
)
_TOKEN = Field(min_length=1, max_length=256)


class ComponentsListParams(Params):
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=16)


class ComponentGetParams(Params):
    component_id: str = _TOKEN
    as_of: str | None = Field(default=None, min_length=1, max_length=64)


class LessonsParams(Params):
    task_class: str = _TOKEN
    deficiency: str = _TOKEN
    as_of_ms: int = Field(default=0, ge=0)


class ReportParams(Params):
    task_ref: str = _TOKEN


class AdoptParams(Params):
    write: HarnessAdoptionWrite


class HarnessReply(Params):
    value: dict[str, Any]


def _cursor(params: Mapping[str, Any]) -> int:
    raw = params.get("cursor")
    if raw is None:
        return 0
    if not str(raw).isdigit():
        raise OperationRefused("INVALID_ARGUMENT", {"field": "cursor"})
    return int(raw)


async def _rows(context: Any, statement: _Statement, text: str) -> list[Any]:
    answer = await engine_send(context, "Uql", statement.request(text))
    body = answer.get("Rows") if isinstance(answer, Mapping) else None
    rows = body.get("rows") if isinstance(body, Mapping) else None
    if not isinstance(rows, list):
        raise unavailable("the engine answered a query with an unknown shape")
    return rows


async def handle_components_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One page of the harness components the caller may read, in id order."""
    statement = _Statement()
    skip = _cursor(params)
    limit = int(params["limit"])
    filters = {"kind": params["kind"]} if params.get("kind") else None
    text = f"MATCH (:HarnessComponent){statement.where(filters)}"
    text += f" |> ORDER BY id |> SKIP {skip} |> LIMIT {limit + 1}"
    rows = await _rows(context, statement, text)
    more = len(rows) > limit
    return {
        "value": {
            "components": rows[:limit],
            "next_cursor": str(skip + limit) if more else None,
        }
    }


async def handle_components_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One component; an absent and a foreign component refuse alike."""
    statement = _Statement()
    seed = f"id = {statement.bind(params['component_id'])}"
    text = f"MATCH (:HarnessComponent) |> WHERE {seed} |> LIMIT 1"
    rows = await _rows(context, statement, text)
    if not rows:
        raise OperationRefused("NOT_FOUND", {"component_id": params["component_id"]})
    return {"value": {"component": rows[0], "as_of": params.get("as_of")}}


async def handle_lessons_retrieve(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's lessons in the engine's confidence order; none abstains."""
    query = {
        "task_class": params["task_class"],
        "deficiency": params["deficiency"],
        "as_of_ms": params["as_of_ms"],
    }
    answer = await engine_send(
        context,
        "DecisionLog",
        {
            "op": {
                "op": "harness_lessons",
                "tenant_id": str(context.caller.tenant),
                "query": query,
            }
        },
    )
    body = dict(answer) if isinstance(answer, Mapping) else {}
    lessons = body.get("lessons")
    if not isinstance(lessons, list):
        raise unavailable("the engine answered a lesson query with an unknown shape")
    reason = body.get("abstain_reason") or ("NoLessons" if not lessons else None)
    return {"value": {"lessons": lessons, "abstain_reason": reason}}


async def handle_evolution_report(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One task's contrast decision. Both reads run as the caller; both or neither."""
    statement = _Statement()
    seed = f"task_ref = {statement.bind(params['task_ref'])}"
    contrast = await _rows(
        context,
        statement,
        f"MATCH (:HarnessContrast) |> WHERE {seed} |> LIMIT 1",
    )
    decisions = _Statement()
    seed = f"task_ref = {decisions.bind(params['task_ref'])}"
    decision = await _rows(
        context,
        decisions,
        f"MATCH (:HarnessContrastDecision) |> WHERE {seed} |> LIMIT 1",
    )
    if not contrast or not decision:
        raise OperationRefused("NOT_FOUND", {"task_ref": params["task_ref"]})
    return {"value": {"contrast": contrast[0], "decision": decision[0]}}


async def handle_evolution_adopt(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Commit one adoption through the engine decision log as the service executor."""
    write = params["write"]
    write = (
        write.model_dump(mode="json") if hasattr(write, "model_dump") else dict(write)
    )
    for field in ("adopted_version_ref", "decision_record_digest"):
        if not str(write.get(field) or "").strip():
            raise OperationRefused("INVALID_ARGUMENT", {"field": field})
    answer = await engine_send(
        context,
        "DecisionLog",
        {
            "op": {
                "op": "harness_adoption",
                "tenant_id": str(context.caller.tenant),
                "write": write,
            }
        },
        idempotent=True,
    )
    return {
        "value": dict(answer) if isinstance(answer, Mapping) else {"result": answer}
    }


def operations() -> tuple[OpSpec, ...]:
    base = "arbora.api.ops.harness."
    return (
        build_read_op(
            op_id="harness.components.list",
            summary="List the harness components the caller may read",
            examples=("list harness components",),
            params=ComponentsListParams,
            result=HarnessReply,
            handler=base + "handle_components_list",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="harness.components.get",
            summary="Read one harness component",
            examples=("show this harness component",),
            params=ComponentGetParams,
            result=HarnessReply,
            handler=base + "handle_components_get",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="harness.lessons.retrieve",
            summary="Retrieve harness lessons for a task class and deficiency",
            examples=("what did we learn about this deficiency",),
            params=LessonsParams,
            result=HarnessReply,
            handler=base + "handle_lessons_retrieve",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="harness.evolution.report",
            summary="Report one task's harness contrast decision",
            examples=("report the harness evolution for this task",),
            params=ReportParams,
            result=HarnessReply,
            handler=base + "handle_evolution_report",
            scope=READ_SCOPE,
        ),
        OpSpec(
            id="harness.evolution.adopt",
            verb=Verb.ACT,
            summary="Commit one harness adoption through the service executor",
            examples=("adopt this harness version",),
            params=AdoptParams,
            result=HarnessReply,
            binding=Composite(handler=base + "handle_evolution_adopt"),
            scopes=frozenset({ADOPT_SCOPE}),
            executor_scopes=frozenset({ADOPT_SCOPE}),
            effect=Effect.WRITE,
            executor=Executor.SERVICE,
            principals=PrincipalRule.SERVICE_ONLY,
            subject=SubjectRef(source=SubjectSource.CALLER_TENANT),
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_components_get",
    "handle_components_list",
    "handle_evolution_adopt",
    "handle_evolution_report",
    "handle_lessons_retrieve",
    "operations",
    "specs",
]
