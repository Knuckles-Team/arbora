"""Revision-bound web-source conformance admission (ARBORA-OPS-R067.10).

Before each ``web_extract`` call, ``require_current_evidence`` reads the engine's
conformance evidence record for the connector (MEDULLA-TYPED-PACKS-R807, served as
``ConnectorPack`` op ``conformance_evidence``) and compares five fields with what fleet
onboarding deployed: tenant, connector, immutable revision, pack digest and the live
tool fingerprints. Missing, expired, revoked or mismatched evidence refuses
``POLICY_DENIED`` before any backend call. The check reads the engine and writes
nothing; no operator flag grants admission.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused

ABSENT = "CONFORMANCE_EVIDENCE_ABSENT"


@dataclass(frozen=True)
class DeployedPack:
    """What fleet onboarding deployed for one connector (from its import receipt)."""

    revision: int
    pack_digest: str
    tool_fingerprints: Mapping[str, str]


def live_tool_fingerprints(entries: Sequence[Any]) -> dict[str, str]:
    """One fingerprint per served tool entry, keyed by its URI (the engine's own digest)."""
    from medulla.connector_pack import _entry_digest
    from medulla.generated.connector_pack import PackEntryKind

    return {
        str(entry.uri): _entry_digest(entry).hex()
        for entry in entries
        if getattr(entry, "kind", None) == PackEntryKind.TOOL
    }


def _denied(reason: str, connector: str) -> OperationRefused:
    return OperationRefused("POLICY_DENIED", {"reason": reason, "connector": connector})


def _now_ms(context: Any) -> int:
    clock = getattr(context, "services", {}).get("now_ms")
    return int(clock()) if callable(clock) else int(time.time() * 1000)


async def _evidence(context: Any, connector: str, tenant: str) -> Mapping[str, Any]:
    from arbora.api.ops.engine_calls import engine_send

    try:
        record = await engine_send(
            context,
            "ConnectorPack",
            {
                "op": {
                    "op": "conformance_evidence",
                    "request": {"connector": connector, "tenant_id": tenant},
                }
            },
        )
    except EngineRefusal as refused:
        if refused.code == ABSENT:
            raise _denied("conformance_evidence_missing", connector) from refused
        raise
    if not isinstance(record, Mapping):
        raise _denied("conformance_evidence_missing", connector)
    return record


def _check_standing(record: Mapping[str, Any], connector: str, now_ms: int) -> None:
    """Refuse a revoked or expired record."""
    if record.get("revoked_at") is not None or record.get("state") == "revoked":
        raise _denied("conformance_evidence_revoked", connector)
    if int(record.get("expires_at", 0)) <= now_ms:
        raise _denied("conformance_evidence_expired", connector)


def _matches(
    record: Mapping[str, Any], tenant: str, connector: str, deployed: DeployedPack
) -> bool:
    """The five compared fields: tenant, connector, revision, pack digest, fingerprints."""
    served = record.get("tool_fingerprints") or {}
    same_identity = (
        record.get("tenant_id") == tenant
        and record.get("connector") == connector
        and record.get("revision") == deployed.revision
        and record.get("pack_digest") == deployed.pack_digest
    )
    return (
        same_identity
        and bool(deployed.tool_fingerprints)
        and all(
            served.get(uri) == digest
            for uri, digest in deployed.tool_fingerprints.items()
        )
    )


async def require_current_evidence(context: Any, connector: str) -> None:
    """Refuse ``POLICY_DENIED`` unless current evidence covers the deployed pack."""
    tenant = str(context.caller.tenant)
    deployments = getattr(context, "services", {}).get("fleet_declarations")
    deployed = deployments.deployed_pack(connector) if deployments else None
    if deployed is None:
        raise _denied("conformance_evidence_missing", connector)
    record = await _evidence(context, connector, tenant)
    _check_standing(record, connector, _now_ms(context))
    if not _matches(record, tenant, connector, deployed):
        raise _denied("conformance_evidence_mismatch", connector)


__all__ = ["DeployedPack", "live_tool_fingerprints", "require_current_evidence"]
