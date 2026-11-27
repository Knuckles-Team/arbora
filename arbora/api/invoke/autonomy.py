"""Block an action until approval when medulla requires confirmation (ARBORA-OPS-R867).

Before the effect of any non-read operation the invoke pipeline asks the engine ``Decide``
with the typed ``AutonomyRequest`` question for the operation id. ``allow`` continues;
``require_confirmation`` refuses ``CONFIRMATION_REQUIRED`` until an approved plan for the
action exists, even for an operation that declares no confirmation of its own. Arbora
treats the verdict as a minimum and never lowers an operation's own ``confirm``. An
unreadable verdict refuses ``UNAVAILABLE``: the gate fails closed and has no knob.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.api.invoke.steps import OpError

#: ``(action class, tenant, irreversible) -> the engine verdict``: ``allow`` or
#: ``require_confirmation`` (the typed ``AutonomyReading`` wire values).
AskVerdict = Callable[[str, str, bool], Awaitable[str]]

ALLOW = "allow"
REQUIRE_CONFIRMATION = "require_confirmation"
QUESTION_KIND = "autonomy_verdict"


async def require_autonomy_clearance(
    ask: AskVerdict | None,
    *,
    op_id: str,
    tenant: str,
    is_read: bool,
    plan_approved: bool,
    irreversible: bool = False,
) -> OpError | None:
    """``None`` when the action may run now, otherwise the refusal."""
    if ask is None or is_read:
        return None
    try:
        verdict = await ask(op_id, tenant, irreversible)
    except Exception:  # noqa: BLE001 - an unreadable verdict is never an allow
        return OpError("UNAVAILABLE", {"reason": "autonomy_verdict_unavailable"})
    if verdict == ALLOW:
        return None
    if verdict == REQUIRE_CONFIRMATION:
        return None if plan_approved else OpError("CONFIRMATION_REQUIRED")
    return OpError("UNAVAILABLE", {"reason": "autonomy_verdict_unavailable"})


#: ``send(method, params, tenant)`` reaches the engine as the host service identity.
VerdictSend = Callable[[str, dict[str, Any], str], Awaitable[Any]]


def autonomy_request(
    tenant: str,
    feature_schema: Mapping[str, Any],
    *,
    op_id: str,
    irreversible: bool,
) -> dict[str, Any]:
    """The engine ``DecideRequest`` of one ``autonomy_verdict`` question.

    The action class is the one declared option. The body is the typed
    ``AutonomyRequest``; an irreversible action also carries the ``irreversible`` safety
    class, so the engine requires confirmation by either rule.
    """
    from medulla.decision_client import DeclaredOption, decide_request, declared, param
    from medulla.generated import models

    body = models.AutonomyRequest(
        irreversibility=(
            models.IrreversibilityClass.IRREVERSIBLE
            if irreversible
            else models.IrreversibilityClass.REVERSIBLE
        )
    ).model_dump(mode="json")
    return decide_request(
        tenant,
        (
            f"autonomy:{op_id}",
            QUESTION_KIND,
            "irreversible" if irreversible else "ordinary",
        ),
        declared([DeclaredOption(op_id)]),
        feature_schema,
        params=[param(QUESTION_KIND, QUESTION_KIND, body)],
    )


def verdict_of(record: Mapping[str, Any]) -> str:
    """The typed verdict of a sealed record.

    An abstention (every recorded outcome of the class is self-reported) earns no
    trust, so it reads as ``require_confirmation``. Any other shape raises.
    """
    from medulla.generated import models

    outcome = record.get("outcome")
    if isinstance(outcome, Mapping) and outcome.get("outcome") == "abstained":
        return REQUIRE_CONFIRMATION
    answer = models.FamilyAnswerAutonomy.model_validate(record["answer"]).value
    return str(getattr(answer.verdict, "value", answer.verdict))


def engine_verdict(
    send: VerdictSend,
    *,
    feature_schema: Callable[[str], Mapping[str, Any]],
    principal: Callable[[], str],
) -> AskVerdict:
    """The production ``AskVerdict`` over the engine ``Decide`` method.

    The host composes it from the same service-identity engine send the approval gate
    question uses (ARBORA-OPS-R867): one door, no second engine path. The record's
    receipt must name this request and the service principal.
    """
    from arbora.deployment.placement import check_receipt, single_record

    async def ask(op_id: str, tenant: str, irreversible: bool) -> str:
        request = autonomy_request(
            tenant, feature_schema(tenant), op_id=op_id, irreversible=irreversible
        )
        # The wire params are ``MethodDecideParams``: the request under ``request``.
        record = single_record(await send("Decide", {"request": request}, tenant))
        check_receipt(record, request, principal())
        return verdict_of(record)

    return ask


def served_autonomy(
    send: VerdictSend,
    *,
    feature_schema: Callable[[str], Mapping[str, Any]],
    principal: Callable[[], str],
) -> dict[str, AskVerdict]:
    """The ``InvokeServices`` change that composes the gate, or none.

    The gate is composed once the pinned engine contract names the ``autonomy_verdict``
    question and the typed ``AutonomyRequest`` (PIN-LAG), so a release without them
    never fails every effect closed.
    """
    from arbora.pin_lag.gates import gate_served

    if not gate_served("autonomy"):
        return {}
    return {
        "autonomy": engine_verdict(
            send, feature_schema=feature_schema, principal=principal
        )
    }


__all__ = [
    "ALLOW",
    "AskVerdict",
    "REQUIRE_CONFIRMATION",
    "autonomy_request",
    "engine_verdict",
    "require_autonomy_clearance",
    "served_autonomy",
    "verdict_of",
]
