"""The medulla capability guard (PIN-LAG: remove after the medulla re-pin).

Arbora pins released medulla. Medulla ``main`` serves engine methods and request
shapes the pinned release lacks. Every Arbora use of such a method goes through this
module, so a pinned release without it refuses ``UNAVAILABLE`` naming exactly the
method or shape that is missing, and nothing falls back to a local stand-in.

Guards held here (delete each with the re-pin that carries it):

- ``ApprovalGateResolve``: the approval gate question at plan mint.
- ``DeployRecordPut`` and ``DeployRecordGet``: the promotion deploy record.
- ``DelegationRecord`` agent-run fields (``kind``, ``run_id``, ``operations``) and
  ``DelegationKind``: the A2A run delegation and its reclaim (``ARBORA-A2A-R001.2.2``).
- ``MutationOutbox`` pull and acknowledge: the approval and trust-flag consumers.
- ``FileDefect``, ``TransitionDefect``, ``RecordRelease``, ``RecordDeployment``,
  ``SpecStatus``, ``LifecycleTrace`` (ARBORA-OPS-R853), ``RetrievalFeedbackRecord``
  (R854), ``QualityRulePut`` and ``ColumnLineage`` (R856), ``ApproveEntityLink``
  (R887): methods engine ``main`` serves and the pinned release lacks. They go through
  :func:`send_main`.
- ``AutonomyRequest``, ``IrreversibilityClass`` and ``FamilyAnswerAutonomy`` (R867): the typed
  autonomy question body and answer; the gate composes only once the contract names them.
- ``QuestionKind`` members ``autonomy_verdict`` (R867), ``tool_relevance`` (R866) and
  ``team_coordination`` (R877): the host composes each gate only once the pinned contract
  names the question (:func:`question_kind_served`).
- ``ResourceProtocol`` members ``ssh``, ``database``, ``web`` and ``rdp``
  (ARBORA-IDENTITY-R019.3): the resource gate sends the real protocol on the wire.
- ``FamilyAnswerCredentialReuse`` (ARBORA-IDENTITY-R023.3): the typed ``credential_reuse``
  ``Decide`` answer.
- ``AuditList`` (ARBORA-OPS-R052.5): the operator-only audit chain read
  (:func:`operator_audit_missing`).
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
    "AUTONOMY_MODELS",
    "AGENT_RUN_DELEGATION_FIELDS",
    "CLAIM_IDENTITY_FIELDS",
    "agent_run_delegation_served",
    "guard_certification_read",
    "guard_skill_upgrade",
    "require_agent_run_delegation",
    "require_delegated_identity",
    "admission_models",
    "require_claim_identity",
    "OUTBOX_MODELS",
    "PinLagUnavailable",
    "accepts_parameter",
    "guard",
    "guard_field",
    "missing_model",
    "operator_audit_missing",
    "question_kind_served",
    "refusal",
    "require_methods",
    "require_enum_member",
    "require_model_field",
    "require_models",
    "require_question_kind",
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
#: The fields an agent-run ``DelegationRecord`` carries (engine ``main``, unreleased).
AGENT_RUN_DELEGATION_FIELDS: tuple[str, ...] = ("kind", "run_id", "operations")


def require_methods(*names: str) -> None:
    """Return once the pinned engine declares every method, else name the first missing."""
    from medulla.generated import METHOD_IDS

    for name in names:
        if name not in METHOD_IDS:
            raise PinLagUnavailable(name)


def question_kind_served(kind: str) -> bool:
    """Whether the pinned engine contract names the ``Decide`` question kind ``kind``.

    The wire kinds are the snake_case members of ``QuestionKind``. A host composes a
    gate over a question only when the pinned contract publishes it, so a release that
    lacks the question never fails every call closed (ARBORA-OPS-R866, R867, R877).
    """
    import typing

    from medulla.generated import models

    members = typing.get_args(models.QuestionKind)
    return any(kind in typing.get_args(member) for member in members)


#: The typed autonomy question body and answer (R867), engine ``main``, unreleased.
AUTONOMY_MODELS: tuple[str, ...] = (
    "AutonomyRequest",
    "IrreversibilityClass",
    "FamilyAnswerAutonomy",
)


def missing_model(names: Iterable[str]) -> str | None:
    """The first request shape the pinned engine lacks, or ``None`` when all are declared."""
    from medulla.generated import models

    return next((name for name in names if not hasattr(models, name)), None)


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


def guard_field(model: str, field: str) -> None:
    """Refuse ``UNAVAILABLE`` naming ``Model.field`` when the pinned shape lacks it."""
    try:
        require_model_field(model, field)
    except PinLagUnavailable as error:
        raise refusal(error) from error


def operator_audit_missing() -> str | None:
    """The first capability the operator-only audit chain lacks, or ``None`` when served.

    The chain is written by ``AuditAppend`` of class ``operator`` and read by
    ``AuditList`` under ``audit:operator`` (ARBORA-OPS-R052.5). A pinned release
    without ``AuditList`` leaves the ``operator_audit`` port inactive and
    ``ops.doctor`` says so by name.
    """
    try:
        require_methods("AuditList")
    except PinLagUnavailable as missing:
        return missing.missing
    return None


def require_enum_member(model: str, value: str) -> None:
    """Return once the pinned enum ``model`` declares ``value``, else name ``Model.value``."""
    from medulla.generated import models

    declared = getattr(models, model, None)
    members = {str(getattr(item, "value", item)) for item in declared or ()}
    if value not in members:
        raise PinLagUnavailable(f"{model}.{value}")


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


def require_agent_run_delegation() -> None:
    """Return once the engine serves agent-run delegations, else name what is missing.

    The record must declare ``kind``, ``run_id`` and ``operations``, and the kind
    enumeration must exist; the claim answer must carry ``delegation_id``.
    """
    from medulla.generated import models

    require_models(("DelegationKind", "DelegationRecord"))
    declared = getattr(models.DelegationRecord, "model_fields", {})
    for name in AGENT_RUN_DELEGATION_FIELDS:
        if name not in declared:
            raise PinLagUnavailable(f"DelegationRecord.{name}")
    claim = getattr(models, "ClaimWorkItemResult", None)
    if "delegation_id" not in getattr(claim, "model_fields", {}):
        raise PinLagUnavailable("ClaimWorkItemResult.delegation_id")


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
    require_model_field("RequestContextClaims", "delegation_id")
    from arbora.security.scope_registry import session_scopes

    if DELEGATE_SCOPE not in session_scopes():
        raise PinLagUnavailable(f"scope {DELEGATE_SCOPE}")


def require_seed_parts() -> None:
    """Return once the engine imports seed parts and receipts them (ARBORA-FLEET-R054).

    PIN-LAG: the seed travels as ``PackEntryKind.SEED_PART`` entries of the pack
    index (MEDULLA-TYPED-PACKS-R104.3), and the import receipt carries
    ``seed.content_digest``. A release without either refuses ``UNAVAILABLE``
    naming it, so a seeded pack is never imported with its seed dropped.
    """
    require_enum_member("PackEntryKind", "seed_part")
    require_model_field("PackImportReceipt", "seed")


#: The request fields an engine component search needs to page one server's tools
#: by name without reading content (ARBORA-FLEET-R073; engine ``main`` lacks both).
COMPONENT_SEARCH_FILTER_FIELDS: tuple[str, ...] = ("server_name", "upstream_name")


def require_component_search_filters() -> None:
    """Return once the engine component search filters by server and name.

    Refuses ``UNAVAILABLE`` naming ``AgentComponentSearchRequest.<field>`` until
    the pinned engine carries it; nothing pages the catalog from a process copy.
    """
    for field in COMPONENT_SEARCH_FILTER_FIELDS:
        require_model_field("AgentComponentSearchRequest", field)


def agent_run_delegation_served() -> bool:
    """Whether the installed engine serves agent-run delegations."""
    try:
        require_agent_run_delegation()
    except PinLagUnavailable:
        return False
    return True


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


def _literal_members(annotation: Any) -> set[str]:
    """Every string a (possibly nested) ``Literal`` union names."""
    import typing

    found: set[str] = set()
    for member in typing.get_args(annotation):
        if typing.get_origin(member) is typing.Literal:
            found.update(str(value) for value in typing.get_args(member))
        else:
            found |= _literal_members(member)
    return found


def require_question_kind(kind: str) -> None:
    """Return once the pinned engine declares the ``Decide`` question ``kind``, else name it.

    PIN-LAG: engine ``main`` serves question kinds the pinned release does not list
    (the wire type also accepts any string, so only the declared members count).
    """
    from medulla.generated import models

    if kind not in _literal_members(getattr(models, "QuestionKind", None)):
        raise PinLagUnavailable(f"QuestionKind.{kind}")
