"""Privacy-safe operation audit payloads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from arbora.api.invoke.plan import params_digest
from arbora.api.invoke.steps import OpError, OpResult, RequestCaller


def audit_event(
    op: Any,
    params: Mapping[str, Any],
    caller: RequestCaller,
    surface: Any,
    status: str,
    audit_ref: str = "",
    evidence: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Record identity and a digest, never argument values.

    ``evidence`` holds identifiers and counts only (screening fields and the
    operator hook decisions); it never holds matched or redacted text.
    """

    event = {
        "op": op.id,
        "surface": surface.value,
        "principal": caller.principal,
        "tenant": caller.tenant,
        "plan_digest": params_digest(params),
        "result_status": status,
        "request_id": caller.request_id,
    }
    if audit_ref:
        event["audit_ref"] = audit_ref
    if evidence:
        event.update(evidence)
    if op.id == "fleet.call":
        server, tool = params.get("server"), params.get("tool")
        if isinstance(server, str) and isinstance(tool, str):
            event["target"] = f"{server}/{tool}"
    if getattr(op.executor, "value", None) == "service":
        event["owner"] = caller.principal
    return event


def run_audit_event(
    run: Any,
    caller: RequestCaller,
    *,
    op: Any = None,
    params: Mapping[str, Any] | None = None,
    result_status: str = "ok",
) -> dict[str, str]:
    """The one event of an orchestrated run (ARBORA-IDENTITY-R026.4).

    Keys: ``trace_id``, ``principal``, ``session_id``, ``run_id``, ``tenant``. A run that
    fails still emits its event with ``result_status``. The event holds no prompt text:
    only identifiers are read from the run.
    """
    from arbora.api.ops.agents import client_session_id

    body = run if isinstance(run, Mapping) else getattr(run, "__dict__", {}) or {}
    run_id = str(body.get("id") or body.get("run_id") or body.get("task_id") or "")
    status = body.get("status")
    state = getattr(status, "state", status)
    event = {
        "op": getattr(op, "id", "agents.run"),
        "trace_id": str(
            body.get("trace_id") or getattr(caller, "request_id", "") or run_id
        ),
        "principal": caller.principal,
        "session_id": client_session_id(caller),
        "run_id": run_id,
        "tenant": caller.tenant,
        "result_status": result_status
        if result_status != "ok"
        else str(getattr(state, "value", state) or result_status),
    }
    return event


@dataclass(frozen=True, slots=True)
class EffectReservation:
    """Durable atomic claim; only acquired may dispatch. No local fallback."""

    reference: str
    state: Literal["acquired", "pending", "completed", "conflict"]
    outcome: OpResult | OpError | None = None


class EffectJournal(Protocol):
    """Owner port for durable deduplication, shared across transports/processes.

    reserve atomically binds a stable tenant/principal/op/idempotency key to
    the request/authority digest, returns completed outcomes for replays, and
    grants at most one acquisition. A changed binding conflicts, never acquires.
    Pending/uncertain effects never reacquire. complete is fenced by the opaque
    acquisition reference, atomically persists an outcome, accepts identical
    completion retries, and rejects conflicting terminal outcomes. A completion
    failure or cancellation leaves the reservation pending for reconciliation.
    This is an existing durable effect owner's port, not a Arbora ledger.
    """

    async def reserve(self, key: str, binding: str) -> EffectReservation: ...

    async def complete(self, reference: str, outcome: OpResult | OpError) -> None: ...
