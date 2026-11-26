"""Evolution loop, schedule and proposal operations (ARBORA-OPS-R022.2.1 to R022.2.5).

The engine keeps every loop's durable state; this module is a facade. Each
operation sends one engine method as the verified caller and returns the
engine's answer unchanged. The loop methods ``LoopStatus``, ``LoopRun`` and
``LoopPause`` are not in the pinned engine release, so they go through the
pin-lag guard: a pinned release refuses ``UNAVAILABLE`` naming the method, and
nothing falls back to a local stand-in.
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
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

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
    "evolution.loops.run": "medulla:LoopRun",
    "evolution.loops.pause": "medulla:LoopPause",
}
READ_SCOPE = "loops:read"
CONTROL_SCOPE = "loops:control"
_LOOP_ID = Field(min_length=1, max_length=256)
_PAGE_LIMIT = Field(default=50, ge=1, le=100)


class LoopRefParams(Params):
    loop_id: str = _LOOP_ID


class EvolutionPageParams(Params):
    cursor: str | None = Field(default=None, min_length=1, max_length=512)
    limit: int = _PAGE_LIMIT


class EvolutionResult(Params):
    value: dict[str, Any]


def _loop_method(method: str) -> None:
    try:
        require_methods(method)
    except PinLagUnavailable as error:
        raise refusal(error) from error


async def handle_loops_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One loop's ``state``, ``last_run_ms`` and ``next_run_ms`` from the engine."""
    _loop_method("LoopStatus")
    answer = await engine_send(context, "LoopStatus", {"loop_id": params["loop_id"]})
    return {"value": _mapping(answer, ("state", "last_run_ms", "next_run_ms"))}


async def handle_loops_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Request one loop run; one idempotency key returns one job id."""
    _loop_method("LoopRun")
    answer = await engine_send(
        context, "LoopRun", {"loop_id": params["loop_id"]}, idempotent=True
    )
    return {"value": _mapping(answer, ("job_id",))}


async def handle_loops_pause(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Pause one loop; pausing a paused loop answers the paused state again."""
    _loop_method("LoopPause")
    answer = await engine_send(
        context, "LoopPause", {"loop_id": params["loop_id"]}, idempotent=True
    )
    return {"value": _mapping(answer, ("state",))}


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
            params=LoopRefParams,
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
