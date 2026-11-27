"""Pack definitions admitted before the import step (ARBORA-FLEET-R002.5, R002.7).

``FleetOnboarding.onboard`` and the catalog refresh call :meth:`DefinitionAdmission.admit`
before the pack import. Each tool, prompt or skill of the captured pack becomes a
:class:`~arbora.api.domain.fleet.definitions.VersionedDefinition`; a higher version over an
admitted one asks the engine ``Decide`` ``SkillUpgrade`` question through the
engine port and applies the answer (``admit_definition``). Any refusal leaves
the prior generation active and no import runs. A success writes one
``DefinitionAdmissionReceipt`` per changed definition into the caller's tenant;
the receipt holds digests and ids, never a body.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from arbora.api.domain.fleet.admission_models import AdmissionRefused
from arbora.api.domain.fleet.definitions import (
    DefinitionAdmissionReceipt,
    DefinitionGeneration,
    DefinitionRefused,
    VersionedDefinition,
    canonical_digest,
    parse_definition,
)
from arbora.api.domain.fleet.engine_admission import SkillUpgradeQuestion
from arbora.fleet.catalog_graph import (
    KG,
    RDF_TYPE,
    _body,
    _json,
    _kind,
    to_ntriples,
)

__all__ = ["DefinitionAdmission", "definitions_from_pack", "receipt_triples"]

_KINDS = frozenset({"tool", "prompt", "skill"})


def _contract_fields(schema: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    required = set(schema.get("required") or ())
    props = schema.get("properties") or {}
    return tuple(
        {
            "name": str(name),
            "required": name in required,
            "type_name": str((spec or {}).get("type", "any")),
        }
        for name, spec in sorted(props.items())
    )


def definitions_from_pack(connector: str, pack: Any) -> dict[str, VersionedDefinition]:
    """The versioned definitions of one captured pack, keyed ``kind:connector/name``."""
    found: dict[str, VersionedDefinition] = {}
    for entry in pack.archive.entries:
        kind = _kind(entry)
        if kind not in _KINDS:
            continue
        body = _body(pack, entry)
        document = _json(body)
        schema = document.get("inputSchema") or {}
        found[f"{kind}:{connector}/{entry.name}"] = parse_definition(
            {
                "kind": kind,
                "producer_id": f"{connector}/{entry.name}",
                "version": document.get("version"),
                "input_schema": schema,
                "output_schema": document.get("outputSchema"),
                "schema_digest": canonical_digest(schema),
                "body_digest": hashlib.sha256(body).hexdigest(),
            }
        )
    return found


def receipt_triples(receipt: DefinitionAdmissionReceipt) -> list[tuple[Any, ...]]:
    """The tenant graph triples of one receipt; no definition body is written."""
    node = f"urn:arbora:definition-admission:{canonical_digest(receipt.model_dump())}"
    triples: list[tuple[Any, ...]] = [
        (node, RDF_TYPE, f"{KG}DefinitionAdmissionReceipt")
    ]
    for name, value in receipt.model_dump().items():
        if value is not None:
            triples.append((node, f"{KG}{name}", (str(value),)))
    return triples


@dataclass
class DefinitionAdmission:
    """The admitted definition generation of one fleet, advanced by whole publishes."""

    #: The engine port: ``async skill_upgrade(tenant, SkillUpgradeQuestion)``.
    port: Any
    #: ``async (ntriples, tenant)`` writes receipts into the caller's tenant graph.
    write: Callable[[str, str], Awaitable[None]] | None = None
    generation: DefinitionGeneration = field(default_factory=DefinitionGeneration)

    async def admit(
        self, tenant: str, connector: str, pack: Any
    ) -> tuple[DefinitionAdmissionReceipt, ...]:
        """Admit every definition of ``pack`` or none; a refusal runs no import."""
        candidates = definitions_from_pack(connector, pack)
        decisions = {}
        assert self.generation.definitions is not None
        for key, candidate in candidates.items():
            prior = self.generation.definitions.get(key)
            if prior is None or candidate.semver <= prior.semver:
                continue
            decisions[key] = await self._decide(tenant, prior, candidate)
        # ``publish`` raises ``DefinitionRefused`` and leaves ``self.generation``.
        published, receipts = self.generation.publish(
            candidates, decisions, tenant=tenant
        )
        self.generation = published
        return receipts

    async def record(
        self, tenant: str, receipts: tuple[DefinitionAdmissionReceipt, ...]
    ) -> None:
        """Write the receipts after the import step; an unwired sink fails closed."""
        if not receipts:
            return
        if self.write is None:
            raise DefinitionRefused(
                "PRODUCER_UNAVAILABLE", {"reason": "no receipt sink"}
            )
        triples = [t for r in receipts for t in receipt_triples(r)]
        await self.write(to_ntriples(triples), tenant)

    async def _decide(
        self, tenant: str, prior: VersionedDefinition, candidate: VersionedDefinition
    ) -> Any:
        question = SkillUpgradeQuestion(
            subject_id=candidate.producer_id,
            subject_kind=candidate.kind,
            previous_fields=_contract_fields(prior.input_schema),
            new_fields=_contract_fields(candidate.input_schema),
            claimed_previous_digest=prior.schema_digest,
            claimed_new_digest=candidate.schema_digest,
        )
        try:
            return await self.port.skill_upgrade(tenant, question)
        except AdmissionRefused as refusal:
            if refusal.code == "PRODUCER_UNAVAILABLE":
                raise DefinitionRefused(
                    "PRODUCER_UNAVAILABLE", refusal.detail
                ) from None
            raise DefinitionRefused(
                "SCHEMA_INCOMPATIBLE", {"engine_code": refusal.code}
            ) from None
