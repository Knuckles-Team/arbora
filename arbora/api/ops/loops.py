"""Loop continuation from the latest committed ``FeedbackLoop`` record (ARBORA-OPS-R876).

``loops.status``, ``loops.run`` and ``loops.pause`` report the continuation the engine
holds: the latest committed ``FeedbackLoop`` record of the loop id, read through the
decision log under the caller. Arbora keeps no continuation state in memory, so two reads
across a restart answer alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._decide import record_of
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import OpSpec, Verb


class LoopParams(Params):
    loop_id: str = Field(min_length=1, max_length=256)


class LoopResult(Params):
    value: dict[str, Any]


async def _continuation(context: Any, loop_id: str) -> dict[str, Any]:
    op = {
        "op": "list",
        "tenant_id": str(context.caller.tenant),
        "kind": "FeedbackLoop",
        "subject": loop_id,
        "limit": 1,
    }
    record = record_of(await engine_send(context, "DecisionLog", {"op": op}))
    return {
        "loop_id": loop_id,
        "continuation": record.get("continuation"),
        "record": record,
    }


async def handle_loops_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {"value": await _continuation(context, params["loop_id"])}


async def handle_loops_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {"value": await _continuation(context, params["loop_id"])}


async def handle_loops_pause(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {"value": await _continuation(context, params["loop_id"])}


#: The engine holds each loop record; a repeat reads the same record.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "loops.run": "medulla:DecisionLog",
    "loops.pause": "medulla:DecisionLog",
}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.ASK,
            op_id="loops.status",
            summary="Report the continuation of one loop",
            examples=("what is this loop's status",),
            params=LoopParams,
            result=LoopResult,
            handler="arbora.api.ops.loops.handle_loops_status",
            scope="loops:read",
        ),
        build_write_op(
            op_id="loops.run",
            summary="Run one loop and report its continuation",
            examples=("run this loop",),
            params=LoopParams,
            result=LoopResult,
            handler="arbora.api.ops.loops.handle_loops_run",
            scope="loops:control",
        ),
        build_write_op(
            op_id="loops.pause",
            summary="Pause one loop and report its continuation",
            examples=("pause this loop",),
            params=LoopParams,
            result=LoopResult,
            handler="arbora.api.ops.loops.handle_loops_pause",
            scope="loops:control",
        ),
    )


specs = operations

__all__ = ["NATIVE_IDEMPOTENCY", "operations", "specs"]
