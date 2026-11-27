"""Block an action until approval when medulla requires confirmation (ARBORA-OPS-R867).

Before the effect of any non-read operation the invoke pipeline asks the engine ``Decide``
with the ``AutonomyVerdict`` question for the operation id. ``Allow`` continues;
``RequireConfirmation`` refuses ``CONFIRMATION_REQUIRED`` until an approved plan for the
action exists, even for an operation that declares no confirmation of its own. Arbora
treats the verdict as a minimum and never lowers an operation's own ``confirm``. An
unreadable verdict refuses ``UNAVAILABLE``: the gate fails closed and has no knob.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from arbora.api.invoke.steps import OpError

#: ``(action class, tenant) -> engine verdict record``.
AskVerdict = Callable[[str, str], Awaitable[dict[str, Any]]]


async def require_autonomy_clearance(
    ask: AskVerdict | None,
    *,
    op_id: str,
    tenant: str,
    is_read: bool,
    plan_approved: bool,
) -> OpError | None:
    """``None`` when the action may run now, otherwise the refusal."""
    if ask is None or is_read:
        return None
    try:
        record = await ask(op_id, tenant)
    except Exception:  # noqa: BLE001 - an unreadable verdict is never an allow
        return OpError("UNAVAILABLE", {"reason": "autonomy_verdict_unavailable"})
    verdict = record.get("verdict") if isinstance(record, dict) else None
    if verdict == "Allow":
        return None
    if verdict == "RequireConfirmation":
        return None if plan_approved else OpError("CONFIRMATION_REQUIRED")
    return OpError("UNAVAILABLE", {"reason": "autonomy_verdict_unavailable"})


#: ``send(method, params, tenant)`` reaches the engine as the host service identity.
VerdictSend = Callable[[str, dict[str, Any], str], Awaitable[Any]]


def engine_verdict(send: VerdictSend) -> AskVerdict:
    """The production ``AskVerdict`` over the engine ``Decide`` method.

    The host composes it from the same service-identity engine send the approval gate
    question uses (ARBORA-OPS-R867): one door, no second engine path.
    """
    from arbora.api.ops._decide import record_of

    async def ask(op_id: str, tenant: str) -> dict[str, Any]:
        request = {
            "tenant_id": tenant,
            "question": {"kind": "autonomy_verdict", "action_class": op_id},
        }
        return record_of(await send("Decide", {"request": request}, tenant))

    return ask


def served_autonomy(send: VerdictSend) -> dict[str, AskVerdict]:
    """The ``InvokeServices`` change that composes the gate, or none.

    The gate is composed once the pinned engine contract names the ``autonomy_verdict``
    question (PIN-LAG), so a release without it never fails every effect closed.
    """
    from arbora.pin_lag.medulla import question_kind_served

    return (
        {"autonomy": engine_verdict(send)}
        if question_kind_served("autonomy_verdict")
        else {}
    )


__all__ = [
    "AskVerdict",
    "engine_verdict",
    "require_autonomy_clearance",
    "served_autonomy",
]
