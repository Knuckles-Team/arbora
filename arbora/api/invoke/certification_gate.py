"""Certification admission before workflow invocation (ARBORA-OPS-R872, R875).

``require_upgrade_admission`` asks the engine ``Decide`` with ``SkillUpgrade`` and reads
the ``SkillUpgradeReceipt``: an abstention refuses ``upgrade_not_admitted``.
``require_attestation`` then compares each atomic skill's ``LocalCertificationAttestation``
by id, revision and digest. Both refuse ``POLICY_DENIED`` before any run effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._decide import decide_record


def _denied(reason: str) -> OperationRefused:
    return OperationRefused("POLICY_DENIED", {"reason": reason})


async def require_upgrade_admission(context: Any, skill: Mapping[str, Any]) -> None:
    """Continue on a compatible or declared-migration upgrade; an abstention refuses."""
    record = await decide_record(context, "skill_upgrade", {"skill": dict(skill)})
    receipt = record.get("receipt") or record
    if receipt.get("outcome") not in ("compatible", "migrated"):
        raise _denied("upgrade_not_admitted")


class SkillRef(BaseModel):
    """One atomic skill of a run: its identity, the revision it is certified at and its digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0)
    digest: str = Field(min_length=1, max_length=128)


#: The most atomic skills one run declares.
MAX_RUN_SKILLS = 32


def _attestation_view(answer: Any) -> Mapping[str, Any] | None:
    """The compared view of a ``CertificationReadAnswer``, or ``None`` for no attestation."""
    stored = answer.get("attestation") if isinstance(answer, Mapping) else None
    if not isinstance(stored, Mapping) or "body" not in stored:
        return None
    body = stored["body"]
    return {
        "skill_id": body["skill"]["component_id"],
        "digest": body["skill"]["definition_digest"],
        "revision": body["completion_revision"],
    }


def engine_attestation_reader(context: Any) -> Any:
    """``read(skill)`` over the engine ``DecisionEval`` ``certification`` read.

    The request is ``{tenant_id, component_id, definition_digest}`` and the engine answers
    ``CertificationReadAnswer {attestation}``: the latest ``LocalCertificationAttestation``
    of that skill at that digest, or null (an absent and a foreign skill answer alike).
    The caller supplies no attestation evidence. The view compared is the skill component
    id and definition digest, and the revision the attestation completed at. Pinned
    releases that lack the read refuse ``UNAVAILABLE`` naming it.
    """
    from arbora.api.ops.pin_guards import send_main

    async def read(skill: Mapping[str, Any]) -> Mapping[str, Any] | None:
        answer = await send_main(
            context,
            "DecisionEval",
            {
                "op": {
                    "op": "certification",
                    "request": {
                        "tenant_id": str(context.caller.tenant),
                        "component_id": skill["id"],
                        "definition_digest": skill["digest"],
                    },
                }
            },
            models=(
                "CertificationReadRequest",
                "CertificationReadAnswer",
                "DecisionEvalOpCertification",
            ),
        )
        return _attestation_view(answer)

    return read


#: The refusal reason when a run's definition cannot be read from the graph.
SOURCE_UNAVAILABLE = "attestation_source_unavailable"


def _definition_body(entry: Any) -> Any:
    """The mapping that holds ``skills``: the entry itself, or its ``registration``/``entry``."""
    if not isinstance(entry, Mapping) or "skills" in entry:
        return entry
    return next((entry[k] for k in ("registration", "entry") if k in entry), entry)


def _component_skills(entry: Any) -> list[dict[str, str]]:
    """The skill refs of one definition node: ``{id, digest}`` per component.

    A ``WorkflowRegistry`` read answers the registration entry, bare or under ``entry``
    or ``registration``; its ``skills`` are ``WorkflowSkill`` refs, the same three fields.
    """
    body = _definition_body(entry)
    skills = body.get("skills") if isinstance(body, Mapping) else None
    if not isinstance(skills, list):
        raise _denied(SOURCE_UNAVAILABLE)
    try:
        return [
            {"id": str(s["component_id"]), "digest": str(s["definition_digest"])}
            for s in skills
        ]
    except (KeyError, TypeError):
        raise _denied(SOURCE_UNAVAILABLE) from None


async def definition_skills(
    context: Any,
    *,
    agents: Sequence[str] = (),
    workflow: Mapping[str, Any] | None = None,
) -> list[dict[str, str]]:
    """The skills the graph says a run uses: its agents' and workflow's definition nodes.

    Each named agent's engine ``AgentLibrary`` head entry and the workflow's
    ``WorkflowRegistry`` record hold the skill refs. A definition that cannot be read, or
    that carries no skill-ref list, refuses ``attestation_source_unavailable``: the gate
    never falls back to what the caller declared (ARBORA-OPS-R872).
    """
    from arbora.api.errors import EngineRefusal
    from arbora.api.ops.engine_calls import engine_send
    from arbora.pin_lag.medulla import PinLagUnavailable

    services = getattr(context, "services", None) or {}
    required: list[dict[str, str]] = []
    try:
        for agent in dict.fromkeys(agents):
            source = services.get("attestation_definition_reader")
            if source is not None:
                entry = await source(agent)
            else:
                from arbora.api.ops.agent_library import current_entry

                entry = await current_entry(context, agent)
            required += _component_skills(entry)
        if workflow is not None:
            record = await engine_send(
                context, "WorkflowRegistry", {"op": "read", "identity": dict(workflow)}
            )
            required += _component_skills(record)
    except (OperationRefused, EngineRefusal, PinLagUnavailable) as failure:
        if isinstance(failure, OperationRefused) and failure.code == "POLICY_DENIED":
            raise
        raise _denied(SOURCE_UNAVAILABLE) from None
    return list(({(s["id"], s["digest"]): s for s in required}).values())


def merge_required(
    required: Sequence[Mapping[str, str]], declared: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The skills to attest: every graph skill, plus any the caller adds.

    A declared skill only ever adds to the graph set. A graph skill the caller does not
    evidence refuses ``attestation_missing``; one it declares at another digest refuses
    ``attestation_mismatch``.
    """
    merged: dict[str, dict[str, Any]] = {}
    by_id = {str(d["id"]): dict(d) for d in declared}
    for skill in required:
        mine = by_id.get(skill["id"])
        if mine is None:
            raise _denied("attestation_missing")
        if mine["digest"] != skill["digest"]:
            raise _denied("attestation_mismatch")
        merged[skill["id"]] = mine
    for name, mine in by_id.items():
        merged.setdefault(name, mine)
    return list(merged.values())


def _declared(skill: SkillRef | Mapping[str, Any]) -> dict[str, Any]:
    return skill.model_dump() if isinstance(skill, SkillRef) else dict(skill)


async def require_run_attestation(
    context: Any,
    skills: Sequence[SkillRef | Mapping[str, Any]] = (),
    *,
    agents: Sequence[str] = (),
    workflow: Mapping[str, Any] | None = None,
) -> None:
    """Gate one run on the current attestation of each of its atomic skills.

    Runs before any run effect of ``agents.run`` and ``agents.plan_run``
    (ARBORA-OPS-R872). The skills come from the graph: the definition nodes of the run's
    named ``agents`` and ``workflow`` (see :func:`definition_skills`). What the caller
    declares only adds to that set, so declaring none never skips the gate. A run that
    names no definition and declares nothing uses no definition-bound skill. The reader
    is the host's ``attestation_reader`` service when composed, else the engine read.
    """
    declared = [_declared(s) for s in skills or ()]
    required = await definition_skills(context, agents=agents, workflow=workflow)
    refs = merge_required(required, declared)
    if not refs:
        return
    services = getattr(context, "services", None) or {}
    read = services.get("attestation_reader") or engine_attestation_reader(context)
    await require_attestation(context, refs, read=read)


async def require_attestation(
    context: Any, skills: Sequence[Mapping[str, Any]], *, read: Any
) -> None:
    """Each skill needs the exact current attestation; ``read(skill)`` reads it."""
    for skill in skills:
        attestation = await read(skill)
        if not attestation:
            raise _denied("attestation_missing")
        if (
            attestation.get("skill_id") != skill["id"]
            or attestation.get("digest") != skill["digest"]
        ):
            raise _denied("attestation_mismatch")
        if attestation.get("revision") != skill["revision"]:
            raise _denied("attestation_stale")


__all__ = [
    "MAX_RUN_SKILLS",
    "SOURCE_UNAVAILABLE",
    "SkillRef",
    "definition_skills",
    "engine_attestation_reader",
    "merge_required",
    "require_attestation",
    "require_run_attestation",
    "require_upgrade_admission",
]
