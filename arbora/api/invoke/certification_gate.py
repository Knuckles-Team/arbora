"""Certification admission before workflow invocation (ARBORA-OPS-R872, R875).

``require_upgrade_admission`` asks the engine ``Decide`` with ``SkillUpgrade`` and reads
the ``SkillUpgradeReceipt``: an abstention refuses ``upgrade_not_admitted``.
``require_attestation`` then compares each atomic skill's ``LocalCertificationAttestation``
by id, revision and digest. Both refuse ``POLICY_DENIED`` before any run effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._decide import decide_record


def _denied(reason: str) -> OperationRefused:
    return OperationRefused("POLICY_DENIED", {"reason": reason})


async def require_upgrade_admission(context: Any, skill: Mapping[str, Any]) -> None:
    """Continue on a compatible or declared-migration upgrade; an abstention refuses."""
    record = await decide_record(context, "SkillUpgrade", {"skill": dict(skill)})
    receipt = record.get("receipt") or record
    if receipt.get("outcome") not in ("compatible", "migrated"):
        raise _denied("upgrade_not_admitted")


async def require_attestation(
    context: Any, skills: Sequence[Mapping[str, Any]], *, read: Any
) -> None:
    """Each skill needs the exact current attestation; ``read(skill_id)`` reads it."""
    for skill in skills:
        attestation = await read(skill["id"])
        if not attestation:
            raise _denied("attestation_missing")
        if (
            attestation.get("skill_id") != skill["id"]
            or attestation.get("digest") != skill["digest"]
        ):
            raise _denied("attestation_mismatch")
        if attestation.get("revision") != skill["revision"]:
            raise _denied("attestation_stale")


__all__ = ["require_attestation", "require_upgrade_admission"]
