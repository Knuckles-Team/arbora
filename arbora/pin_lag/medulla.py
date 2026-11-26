"""The medulla capability guard (PIN-LAG: remove after the medulla re-pin).

Arbora pins released medulla. Medulla ``main`` serves engine methods and request
shapes the pinned release lacks. Every Arbora use of such a method goes through this
module, so a pinned release without it refuses ``UNAVAILABLE`` naming exactly the
method or shape that is missing, and nothing falls back to a local stand-in.

Guards held here (delete each with the re-pin that carries it):

- ``ApprovalGateResolve``: the approval gate question at plan mint.
- ``DeployRecordPut`` and ``DeployRecordGet``: the promotion deploy record.
- ``MutationOutbox`` pull and acknowledge: the approval and trust-flag consumers.
- ``FileDefect``, ``TransitionDefect``, ``RecordRelease``, ``RecordDeployment``,
  ``SpecStatus``, ``LifecycleTrace`` (ARBORA-OPS-R853), ``RetrievalFeedbackRecord``
  (R854), ``QualityRulePut`` and ``ColumnLineage`` (R856), ``ApproveEntityLink``
  (R887): methods engine ``main`` serves and the pinned release lacks. They go through
  :func:`send_main`.
- ``MethodSparqlParams.graph_media_type`` (R857.3): the graph body serialized by the engine.
- ``IdentityOpWireLoginGrant`` and ``IdentityOpWireLoginRevoke`` (R858),
  ``JobKindProgramOptimize`` and ``LearningWriteMovePointer`` (R860): request shapes the
  pinned release lacks, checked by :func:`require_models`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._engine import engine_send
from arbora.pin_lag.unavailable import PinLagUnavailable

__all__ = [
    "ADMISSION_MODELS",
    "CLAIM_IDENTITY_FIELDS",
    "admission_models",
    "require_claim_identity",
    "OUTBOX_MODELS",
    "PinLagUnavailable",
    "accepts_parameter",
    "guard",
    "refusal",
    "require_methods",
    "require_model_field",
    "require_models",
    "send_main",
]

#: The request shapes of the engine outbox pull and acknowledge.
OUTBOX_MODELS: tuple[str, ...] = ("MutationOutboxOpPull", "MutationOutboxOpAck")

#: The typed ``Decide`` answers the confirm deciders read, and the identity digests a
#: ``ClaimWorkItem`` result carries for the run trace (engine ``main``, unreleased).
ADMISSION_MODELS: tuple[str, ...] = (
    "ToolActionAnswer",
    "HeldItemAnswer",
    "FamilyAnswerToolActionAdmission",
    "FamilyAnswerHeldItemAdmission",
)
CLAIM_IDENTITY_FIELDS: tuple[str, ...] = (
    "capability_digest",
    "catalog_digest",
    "model_digest",
    "policy_digest",
    "delegator_id",
    "selected_agent_id",
    "delegation_id",
)


def require_methods(*names: str) -> None:
    """Return once the pinned engine declares every method, else name the first missing."""
    from medulla.generated import METHOD_IDS

    for name in names:
        if name not in METHOD_IDS:
            raise PinLagUnavailable(name)


def require_models(names: Iterable[str]) -> None:
    """Return once the pinned engine declares every request shape, else name the first missing."""
    from medulla.generated import models

    for name in names:
        if not hasattr(models, name):
            raise PinLagUnavailable(name)


def require_model_field(model: str, field: str) -> None:
    """Return once the pinned request shape carries ``field``, else name ``Model.field``."""
    from medulla.generated import models

    declared = getattr(getattr(models, model, None), "model_fields", {})
    if field not in declared:
        raise PinLagUnavailable(f"{model}.{field}")


def accepts_parameter(method: Any, name: str) -> bool:
    """Whether the installed client method takes ``name`` (a request field the pin may lack).

    ``AuditAppend`` gained ``evidence`` on the engine's main branch; until the
    pin carries it the field is withheld and this answers ``False``.
    """
    import inspect

    return name in inspect.signature(method).parameters


def refusal(error: PinLagUnavailable) -> OperationRefused:
    """The served refusal of a missing capability: ``UNAVAILABLE`` naming it."""
    details: dict[str, Any] = {
        "reason": f"the installed engine does not serve {error.missing}",
        "missing": error.missing,
    }
    return OperationRefused("UNAVAILABLE", details)


def admission_models() -> Any:
    """The typed admission answer models, or refuse naming the first one missing."""
    from types import SimpleNamespace

    require_models(ADMISSION_MODELS)
    from medulla.generated import models

    return SimpleNamespace(**{name: getattr(models, name) for name in ADMISSION_MODELS})


def require_claim_identity() -> None:
    """Return once the claim answer carries every identity digest, else name the first missing."""
    from medulla.generated import models

    claim = getattr(models, "ClaimWorkItemResult", None)
    if claim is None:
        raise PinLagUnavailable("ClaimWorkItemResult")
    declared = getattr(claim, "model_fields", {})
    for name in CLAIM_IDENTITY_FIELDS:
        if name not in declared:
            raise PinLagUnavailable(f"ClaimWorkItemResult.{name}")


def guard(*methods: str, models: Iterable[str] = ()) -> None:
    """Refuse ``UNAVAILABLE`` naming the first method or request shape the pin lacks."""
    try:
        require_methods(*methods)
        require_models(models)
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
    guard is the whole difference from :func:`arbora.api.ops._engine.engine_send`; delete
    the call's ``models`` and this wrapper with the re-pin that carries them.
    """
    guard(method, models=models)
    return await engine_send(context, method, params, idempotent=idempotent)
