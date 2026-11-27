"""Skill hook events received as OpenTelemetry logs (ARBORA-OPS-R953).

``skills.telemetry.ingest`` maps an OTLP/JSON logs body to skill invocation
events and forwards them to the engine ``SkillTelemetry`` ``Ingest``. The
verified principal is the user; a body never names one, and a human caller
sends no pseudonym (the engine derives it). A run the caller does not own
refuses ``TELEMETRY_SESSION_UNKNOWN`` exactly like an absent run.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_write_op
from arbora.api.ops.engine_calls import engine_graph_op
from arbora.api.ops.knowledge import EngineReply
from arbora.api.otlp_logs import MAX_RECORDS, TelemetryFieldUnknown, events_from_logs
from arbora.api.registry import HttpShape, OpSpec, Verb

SCOPE = "telemetry:write"
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "skills.telemetry.ingest": "medulla:SkillTelemetry",
    }
)


class TelemetryIngestParams(Params):
    resourceLogs: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_RECORDS)  # noqa: N815


async def handle_skill_telemetry_ingest(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    try:
        events = events_from_logs(str(context.caller.tenant), params["resourceLogs"])
    except TelemetryFieldUnknown:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"reason": "TELEMETRY_FIELD_UNKNOWN"}
        ) from None
    answer = await engine_graph_op(
        context,
        "SkillTelemetry",
        {"op": "ingest", "events": events},
        idempotent=True,
    )
    return {"value": answer}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            verb=Verb.WRITE,
            op_id="skills.telemetry.ingest",
            summary="Report skill use as OpenTelemetry logs",
            examples=("report that a skill ran",),
            params=TelemetryIngestParams,
            result=EngineReply,
            handler="arbora.api.ops.skills_telemetry.handle_skill_telemetry_ingest",
            scope=SCOPE,
            http=HttpShape(method="POST", path="/api/v1/logs"),
        ),
    )


specs = operations

__all__ = ["NATIVE_IDEMPOTENCY", "handle_skill_telemetry_ingest", "operations", "specs"]
