"""Evolution loop, schedule and proposal operations (ARBORA-OPS-R022.2.1 to R022.2.5).

The engine keeps every loop's durable state; this module is a facade. Each
operation sends one existing engine method as the verified caller and returns
the engine's answer. Status reads the ``Loops`` ``get`` view, run submits an
``AnalyticsJob`` ``Submit`` and pause is ``AnalyticsJob`` ``SchedulePause``;
the engine has no separate loop methods (it retired them as duplicates). A
pinned release that lacks one of these shapes refuses ``UNAVAILABLE`` naming
it through the pin-lag guard.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send
from arbora.api.registry import OpSpec
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_models

__all__ = [
    "handle_loops_pause",
    "handle_loops_run",
    "handle_loops_status",
    "handle_proposals_list",
    "handle_schedules_list",
    "operations",
]

#: The engine replays each write by the idempotency key the pipeline forwards.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "evolution.loops.run": "medulla:AnalyticsJob.Submit",
    "evolution.loops.pause": "medulla:AnalyticsJob.SchedulePause",
}
READ_SCOPE = "loops:read"
CONTROL_SCOPE = "loops:control"
_LOOP_ID = Field(min_length=1, max_length=256)
_PAGE_LIMIT = Field(default=50, ge=1, le=100)


class LoopRefParams(Params):
    loop_id: str = _LOOP_ID


class LoopRunParams(LoopRefParams):
    #: The engine ``JobKind`` of the run, passed to ``Submit`` unchanged.
    kind: dict[str, Any]


class EvolutionPageParams(Params):
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = _PAGE_LIMIT


class EvolutionResult(Params):
    value: dict[str, Any]


def _require_shape(*names: str) -> None:
    try:
        require_models(names)
    except PinLagUnavailable as error:
        raise refusal(error) from error


async def handle_loops_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One loop's ``state``, ``last_run_ms`` and ``next_run_ms`` from the engine."""
    _require_shape("LoopOpGet")
    answer = await engine_send(
        context, "Loops", {"op": {"op": "get", "loop_id": params["loop_id"]}}
    )
    instance = answer.get("instance") if isinstance(answer, Mapping) else None
    if not isinstance(instance, Mapping) or "state" not in instance:
        raise OperationRefused("UNAVAILABLE", {"reason": "unexpected engine answer"})
    moments = [
        step["at_ms"]
        for step in answer.get("transitions", ())
        if isinstance(step, Mapping) and isinstance(step.get("at_ms"), int)
    ]
    return {
        "value": {
            "state": instance["state"],
            "last_run_ms": max(moments) if moments else None,
            # A next-run instant is derived from the schedule tick history.
            "next_run_ms": None,
        }
    }


async def handle_loops_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Request one loop run as a submitted job; one key returns one job id."""
    _require_shape("JobOpSubmit")
    graph = await caller_dispatch_graph(context)
    spec = {"graph": graph, "kind": params["kind"], "purpose": params["loop_id"]}
    answer = await engine_send(
        context, "AnalyticsJob", {"op": {"Submit": spec}}, idempotent=True
    )
    job_id = (
        answer.get("id", answer.get("job_id")) if isinstance(answer, Mapping) else None
    )
    if not isinstance(job_id, str):
        raise OperationRefused("UNAVAILABLE", {"reason": "unexpected engine answer"})
    return {"value": {"job_id": job_id}}


async def handle_loops_pause(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Pause one loop's schedule; pausing a paused loop answers paused again."""
    _require_shape("JobOpSchedulePause")
    graph = await caller_dispatch_graph(context)
    answer = await engine_send(
        context,
        "AnalyticsJob",
        {"op": {"SchedulePause": {"name": params["loop_id"], "graph": graph}}},
        idempotent=True,
    )
    return {"value": {"state": _mapping(answer, ("state",))["state"]}}


def _mapping(answer: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """The engine's reply restricted to its declared keys; anything else is refused."""
    if not isinstance(answer, Mapping) or not all(key in answer for key in keys):
        raise OperationRefused("UNAVAILABLE", {"reason": "unexpected engine answer"})
    return {key: answer[key] for key in keys}


def _next_run(item: Mapping[str, Any]) -> int:
    value = item.get("next_run_ms", item.get("next_run_after_ms"))
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _identity(item: Mapping[str, Any]) -> str:
    return str(item.get("id", item.get("name", "")))


def _decode_cursor(cursor: str | None) -> tuple[int, str] | None:
    if cursor is None:
        return None
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return int(raw[0]), str(raw[1])
    except (ValueError, TypeError, IndexError, binascii.Error):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "cursor"}) from None


def _encode_cursor(item: Mapping[str, Any]) -> str:
    raw = json.dumps([_next_run(item), _identity(item)]).encode()
    return base64.urlsafe_b64encode(raw).decode()


async def handle_schedules_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """This tenant's loop schedules ordered by next run then id, paged by cursor."""
    graph = await caller_dispatch_graph(context)
    answer = await engine_send(
        context, "AnalyticsJob", {"op": {"ScheduleList": {"graph": graph}}}
    )
    rows = (
        answer.get("schedules", answer.get("items"))
        if isinstance(answer, Mapping)
        else answer
    )
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise OperationRefused("UNAVAILABLE", {"reason": "unexpected engine answer"})
    ordered = sorted(rows, key=lambda row: (_next_run(row), _identity(row)))
    after = _decode_cursor(params.get("cursor"))
    if after is not None:
        ordered = [row for row in ordered if (_next_run(row), _identity(row)) > after]
    page = ordered[: params["limit"]]
    more = len(ordered) > len(page)
    return {
        "value": {
            "items": [dict(row) for row in page],
            "next_cursor": _encode_cursor(page[-1]) if more else None,
        }
    }


async def handle_proposals_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """This tenant's revision proposals with state and digest, paged by cursor."""
    request: dict[str, Any] = {"limit": params["limit"]}
    if params.get("cursor") is not None:
        request["cursor"] = params["cursor"]
    answer = await engine_send(context, "RevisionProposalList", {"request": request})
    rows = answer.get("rows") if isinstance(answer, Mapping) else None
    if not isinstance(rows, list):
        raise OperationRefused("UNAVAILABLE", {"reason": "unexpected engine answer"})
    items = [
        {"id": row["id"], "state": row["stage"], "digest": row["digest"]}
        for row in rows
        if isinstance(row, Mapping)
    ]
    return {"value": {"items": items, "next_cursor": answer.get("next_cursor")}}


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.evolution"
    return (
        build_read_op(
            op_id="evolution.loops.status",
            summary="Show one evolution loop's state and run times",
            examples=("what is this loop doing",),
            params=LoopRefParams,
            result=EvolutionResult,
            handler=f"{module}.handle_loops_status",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="evolution.schedules.list",
            summary="List this tenant's loop schedules by next run",
            examples=("list the loop schedules",),
            params=EvolutionPageParams,
            result=EvolutionResult,
            handler=f"{module}.handle_schedules_list",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="evolution.proposals.list",
            summary="List this tenant's revision proposals",
            examples=("list the evolution proposals",),
            params=EvolutionPageParams,
            result=EvolutionResult,
            handler=f"{module}.handle_proposals_list",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="evolution.loops.run",
            summary="Request one run of an evolution loop",
            examples=("run this loop now",),
            params=LoopRunParams,
            result=EvolutionResult,
            handler=f"{module}.handle_loops_run",
            scope=CONTROL_SCOPE,
        ),
        build_write_op(
            op_id="evolution.loops.pause",
            summary="Pause one evolution loop",
            examples=("pause this loop",),
            params=LoopRefParams,
            result=EvolutionResult,
            handler=f"{module}.handle_loops_pause",
            scope=CONTROL_SCOPE,
        ),
    )
