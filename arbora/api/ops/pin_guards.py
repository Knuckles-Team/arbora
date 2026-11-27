"""The served refusals of the medulla pin-lag guards (PIN-LAG: remove after the re-pin).

:mod:`arbora.pin_lag.medulla` names what the pinned engine lacks and raises
``PinLagUnavailable``; this module turns that into the served ``UNAVAILABLE``
refusal and sends the guarded engine ``main`` methods. It sits in ``api`` because
the refusal type and the engine send belong to the operation layer; ``pin_lag``
stays a leaf of upstream-shape checks that imports nothing of Arbora.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import engine_send
from arbora.pin_lag import medulla as pins
from arbora.pin_lag.unavailable import PinLagUnavailable

__all__ = [
    "DELEGATE_SCOPE",
    "guard",
    "guard_certification_read",
    "guard_field",
    "guard_held_item_critique",
    "guard_skill_upgrade",
    "refusal",
    "require_delegated_identity",
    "send_main",
]


def guard_field(model: str, field: str) -> None:
    """Refuse ``UNAVAILABLE`` naming ``Model.field`` when the pinned shape lacks it."""
    try:
        pins.require_model_field(model, field)
    except PinLagUnavailable as error:
        raise refusal(error) from error


def refusal(error: PinLagUnavailable) -> OperationRefused:
    """The served refusal of a missing capability: ``UNAVAILABLE`` naming it."""
    details: dict[str, Any] = {
        "reason": f"the installed engine does not serve {error.missing}",
        "missing": error.missing,
    }
    return OperationRefused("UNAVAILABLE", details)


def guard(*methods: str, models: Iterable[str] = ()) -> None:
    """Refuse ``UNAVAILABLE`` naming the first method or request shape the pin lacks."""
    try:
        pins.require_methods(*methods)
        pins.require_models(models)
    except PinLagUnavailable as error:
        raise refusal(error) from error


async def send_main(
    context: Any,
    method: str,
    params: dict[str, Any],
    *,
    idempotent: bool = False,
    models: Iterable[str] = (),
) -> Any:
    """Send one engine ``main`` method as the verified caller, or refuse ``UNAVAILABLE``.

    The refusal names the first method or request shape the pinned release lacks. The
    guard is the whole difference from :func:`arbora.api.ops.engine_calls.engine_send`; delete
    the call's ``models`` and this wrapper with the re-pin that carries them.
    """
    guard(method, models=models)
    return await engine_send(context, method, params, idempotent=idempotent)


#: The service-only host scope that lets a host act under an engine delegation
#: (MEDULLA-IDENTITY-R744; engine ``main``, unreleased).
DELEGATE_SCOPE = "identity:delegate"


def require_delegated_identity() -> None:
    """Return once the engine serves the acting delegation, else name what is missing.

    PIN-LAG: two pieces must exist. The envelope claim shape must declare
    ``RequestContextClaims.delegation_id``, and the engine's served scope registry
    must carry ``identity:delegate``. A release without either cannot run a call as
    a delegation's principal, so nothing sends the claim.
    """
    pins.require_model_field("RequestContextClaims", "delegation_id")
    from arbora.security.scope_registry import session_scopes

    if DELEGATE_SCOPE not in session_scopes():
        raise PinLagUnavailable(f"scope {DELEGATE_SCOPE}")


def guard_skill_upgrade() -> None:
    """Refuse ``UNAVAILABLE`` naming what a ``SkillUpgrade`` ``Decide`` needs (R034.13).

    PIN-LAG: engine ``main`` answers ``Decide`` with ``QuestionKind::SkillUpgrade`` and
    declares ``SkillUpgradeRequest``; the pinned release may lack the shape.
    """
    guard("Decide", models=("SkillUpgradeRequest", "SkillUpgradeReceipt"))


def guard_certification_read() -> None:
    """Refuse ``UNAVAILABLE`` naming what reading a stored attestation needs (R034.14).

    PIN-LAG: ``DecisionEval`` ``CertifyLocalSkill`` and its
    ``LocalCertificationAttestation`` are engine ``main`` shapes. The by-digest read
    op ``certification`` (``{tenant_id, digest}``, answering the attestation or null,
    MEDULLA-DECISION-ENGINE-R142.103) is decided for engine ``main`` and is not served
    by the pinned release, so the guard refuses until the contract declares it.
    """
    guard(
        "DecisionEval",
        models=("LocalCertificationAttestation", "DecisionEvalOpCertification"),
    )


def guard_held_item_critique() -> None:
    """Refuse ``UNAVAILABLE`` naming what recording a critique needs (ARBORA-A2A-R022.1).

    PIN-LAG: the ``DecisionEval`` op ``held_item_critique`` is an engine ``main`` shape
    (MEDULLA-DECISION-ENGINE-R038.6); the pinned release may not declare it.
    """
    guard(
        "DecisionEval",
        models=("DecisionEvalOpHeldItemCritique", "HeldItemCritiqueRequest"),
    )
