"""Admitted ingest maintenance (ARBORA-OPS-R886).

``ingest.maintenance.run`` measures the corpus's ingest throughput, asks the engine
``Decide`` with the ``IngestShortfall`` question what to do, and applies each admitted
action under the owner's authorization, the expected revision and the cost ceiling. All
three are checked before each effect; a missing authorization refuses before any engine
call. Arbora keeps no maintenance state: the engine decides and records.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.pin_guards import send_main


def _record(answer: Any) -> Mapping[str, Any]:
    if isinstance(answer, list):
        answer = answer[0] if answer else {}
    return answer if isinstance(answer, Mapping) else {}


async def run_maintenance(
    context: Any, params: Mapping[str, Any], op: Any
) -> dict[str, Any]:
    """Measure, decide, then apply each admitted action within the ceiling."""
    authorization = params.get("owner_authorization")
    if not authorization:
        raise OperationRefused(
            "POLICY_DENIED", {"reason": "owner_authorization_missing"}
        )
    measured = _record(
        await send_main(
            context,
            "MeasureIngestThroughput",
            {"corpus": params["corpus"], "window_end_ms": params["window_end_ms"]},
        )
    )
    decision = _record(
        await send_main(
            context,
            "Decide",
            {
                "request": {
                    "tenant_id": str(context.caller.tenant),
                    "question": {
                        "kind": "IngestShortfall",
                        "corpus": params["corpus"],
                        "measurement": dict(measured),
                    },
                }
            },
        )
    )
    remaining = int(params["cost_ceiling"])
    applied: list[dict[str, Any]] = []
    for action in decision.get("admitted_actions") or []:
        if int(action.get("revision", -1)) != int(params["expected_revision"]):
            raise EngineRefusal("REVISION_CONFLICT")
        cost = int(action.get("cost", 0))
        if cost > remaining:
            raise OperationRefused("POLICY_DENIED", {"reason": "cost_ceiling"})
        remaining -= cost
        await send_main(
            context,
            str(action["method"]),
            {
                **dict(action.get("params") or {}),
                "authorization": authorization,
                "expected_revision": int(params["expected_revision"]),
            },
            idempotent=True,
        )
        applied.append({"method": action["method"], "cost": cost})
    return {"value": {"applied": applied, "cost_remaining": remaining}}


__all__ = ["run_maintenance"]
