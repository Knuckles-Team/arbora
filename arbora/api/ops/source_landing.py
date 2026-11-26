"""Server-side landing policy: fast gates first, verdicts to the engine (ARBORA-OPS-R849).

``source.land`` runs the fast gates against one submitted changeset through the
``GateRunner`` port, records every verdict in the engine with ``RecordGateVerdict``
and refuses ``POLICY_DENIED`` when any gate failed or timed out. The engine's
landing policy accepts the changeset from the recorded verdicts; arbora keeps
no queue and decides no revert.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._engine import engine_send
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

#: Seconds one fast-gate run may take; a timeout counts as a failed gate.
DEFAULT_FAST_GATE_TIMEOUT_S = 300
FAST_GATE_TIMEOUT_RANGE = (30, 3600)
FAST_GATES = ("format", "hygiene", "secrets", "types", "tests-fast")


class GateResult(BaseModel):
    """One gate's verdict over the materialized changeset tree."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str = Field(min_length=1, max_length=64)
    passed: bool
    input_digest: str = Field(min_length=1, max_length=128)
    evidence_digest: str = Field(min_length=1, max_length=128)
    duration_ms: int = Field(ge=0)


@runtime_checkable
class GateRunner(Protocol):
    """Runs the fast gates against one changeset tree; the default calls the
    repository-manager connector's gate tool through the fleet."""

    async def run_fast_gates(
        self, *, repository: str, branch: str, changeset: str, context: ExecutionContext
    ) -> Sequence[GateResult]: ...


def fast_gate_timeout(context: Any) -> int:
    configured = context.services.get("source_land_fast_gate_timeout_s")
    low, high = FAST_GATE_TIMEOUT_RANGE
    if configured is None:
        return DEFAULT_FAST_GATE_TIMEOUT_S
    if type(configured) is not int or not low <= configured <= high:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"field": "source.land.fast_gate_timeout_s"}
        )
    return configured


async def land_changeset(
    context: Any, *, repository: str, branch: str, changeset: str
) -> dict[str, Any]:
    """Gate, record and report; nothing is recorded unless every gate ran."""
    runner = context.services.get("gate_runner")
    if runner is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "gate runner is not composed"})
    try:
        require_methods("RecordGateVerdict")
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    timeout = fast_gate_timeout(context)
    try:
        results = await asyncio.wait_for(
            runner.run_fast_gates(
                repository=repository,
                branch=branch,
                changeset=changeset,
                context=context,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        raise OperationRefused(
            "POLICY_DENIED", {"reason": "fast_gate_failed", "gate": "timeout"}
        ) from None
    verdicts = [GateResult.model_validate(result) for result in results]
    for verdict in verdicts:
        await engine_send(
            context,
            "RecordGateVerdict",
            {
                "changeset": changeset,
                "branch": branch,
                "gate_id": verdict.gate_id,
                "input_digest": verdict.input_digest,
                "verdict": "pass" if verdict.passed else "fail",
                "evidence_digest": verdict.evidence_digest,
                "duration_ms": verdict.duration_ms,
            },
            idempotent=False,
        )
    failed = next((v.gate_id for v in verdicts if not v.passed), None)
    absent = [g for g in FAST_GATES if g not in {v.gate_id for v in verdicts}]
    if failed is not None:
        raise OperationRefused(
            "POLICY_DENIED", {"reason": "fast_gate_failed", "gate": failed}
        )
    if absent:
        raise OperationRefused(
            "POLICY_DENIED", {"reason": "fast_gate_failed", "gate": absent[0]}
        )
    return {
        "changeset": changeset,
        "branch": branch,
        "verdicts": [v.model_dump(mode="json") for v in verdicts],
    }


__all__ = [
    "DEFAULT_FAST_GATE_TIMEOUT_S",
    "FAST_GATES",
    "GateResult",
    "GateRunner",
    "fast_gate_timeout",
    "land_changeset",
]
