"""The one start-up audit event that names the inactive gates (PIN-LAG).

A pinned engine that lacks the autonomy, tool-relevance or team-coordination question
leaves that gate uncomposed. The host records the inactive set once per process as an
audit event, so the absence of a safety gate is on the record and not only in a probe.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Coroutine
from typing import Any

from arbora.pin_lag.gates import inactive_gate_lines, inactive_gates

__all__ = ["GATE_INACTIVE_OP", "gate_inactive_event", "gate_inactive_startup"]

#: The audit operation of the event; the inactive gate names follow after a colon.
GATE_INACTIVE_OP = "host.gate_inactive"


def gate_inactive_event(tenant: str, principal: str, request_id: str) -> dict[str, str]:
    """The audit event naming every inactive gate, or an empty mapping when none is."""
    names = inactive_gates()
    if not names:
        return {}
    digest = hashlib.sha256("\n".join(inactive_gate_lines()).encode()).hexdigest()
    return {
        "tenant": tenant,
        "principal": principal,
        "op": f"{GATE_INACTIVE_OP}:{','.join(names)}",
        "surface": "console",
        "plan_digest": digest,
        "request_id": request_id,
    }


def gate_inactive_startup(
    adapter: Any, session: Callable[[], Any]
) -> Callable[[], Coroutine[Any, Any, None]]:
    """The process start-up step that appends the event once (no event when none inactive)."""
    from arbora.api.mcp.caller import caller_from_session
    from arbora.api.registry import AuditClass

    async def record() -> None:
        probe = inactive_gates()
        if not probe:
            return
        digest = hashlib.sha256(",".join(probe).encode()).hexdigest()[:16]
        caller = caller_from_session(
            session(), credential_kind="service", request_id=f"gate-inactive-{digest}"
        )
        event = gate_inactive_event(caller.tenant, caller.principal, caller.request_id)
        reference = await adapter.preflight(event, AuditClass.EVENT, caller)
        await adapter.write(
            {**event, "audit_ref": reference, "result_status": "OK"},
            AuditClass.EVENT,
            caller,
        )

    return record
