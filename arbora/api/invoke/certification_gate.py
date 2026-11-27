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
    """One atomic skill of a run: its identity, revision, digest and stored attestation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0)
    digest: str = Field(min_length=1, max_length=128)
    attestation_digest: str = Field(min_length=1, max_length=128)


#: The most atomic skills one run declares.
MAX_RUN_SKILLS = 32


def engine_attestation_reader(context: Any) -> Any:
    """``read(skill)`` over the engine ``DecisionEval`` ``certification`` read.

    The engine answers the stored ``LocalCertificationAttestation`` by digest, or null
    for an absent or foreign digest (an absent and a foreign digest are the same
    answer). The view compared is the skill component id and definition digest, and the
    revision the attestation completed at. Pinned releases that lack the read refuse
    ``UNAVAILABLE`` naming it.
    """
    from arbora.pin_lag.medulla import send_main

    async def read(skill: Mapping[str, Any]) -> Mapping[str, Any] | None:
        stored = await send_main(
            context,
            "DecisionEval",
            {
                "op": {
                    "op": "certification",
                    "request": {
                        "tenant_id": str(context.caller.tenant),
                        "digest": skill["attestation_digest"],
                    },
                }
            },
            models=("LocalCertificationAttestation", "DecisionEvalOpCertification"),
        )
        if not isinstance(stored, Mapping) or "body" not in stored:
            return None
        body = stored["body"]
        return {
            "skill_id": body["skill"]["component_id"],
            "digest": body["skill"]["definition_digest"],
            "revision": body["completion_revision"],
        }

    return read


async def require_run_attestation(
    context: Any, skills: Sequence[SkillRef | Mapping[str, Any]] = ()
) -> None:
    """Gate one run on the current attestation of each declared atomic skill.

    Runs before any run effect of ``agents.run`` and ``agents.plan_run``
    (ARBORA-OPS-R872). A run that declares no skill has nothing to attest. The reader is
    the host's ``attestation_reader`` service when composed, else the engine read.
    """
    if not skills:
        return
    refs = [s.model_dump() if isinstance(s, SkillRef) else dict(s) for s in skills]
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
    "SkillRef",
    "engine_attestation_reader",
    "require_attestation",
    "require_run_attestation",
    "require_upgrade_admission",
]
