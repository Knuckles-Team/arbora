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


def engine_verdict(context_for: Callable[[str], Any]) -> AskVerdict:
    """The production ``AskVerdict`` over the engine ``Decide`` method."""
    from arbora.api.ops._decide import decide_record

    async def ask(op_id: str, tenant: str) -> dict[str, Any]:
        return await decide_record(
            context_for(tenant), "AutonomyVerdict", {"action_class": op_id}
        )

    return ask


__all__ = ["AskVerdict", "engine_verdict", "require_autonomy_clearance"]
