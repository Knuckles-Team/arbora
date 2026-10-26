"""Register connector access contracts as unapproved virtual mappings.

A fleet connector's ``ontology://`` resources may declare access contracts.
Each contract binds one ontology class to one live source operation.
Onboarding registers one ``SourceConnection`` per source and one unapproved
``VirtualMapping`` per contract. The virtual catalog serves a mapping only
after approval, so registration exposes no data.

The SDK parser ships in ``agent_connector_sdk.access_contract``. Without that
module, onboarding skips contracts and logs at debug level.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from arbora.api.errors import not_served
from arbora.fleet.declarations import entry_bytes

logger = logging.getLogger(__name__)

#: SDK access kind to AU virtual-graph source kind.
SOURCE_KIND_BY_ACCESS = {
    "mcp_tool": "mcp",
    "http_endpoint": "api",
    "graphql_query": "graphql",
    "a2a_skill": "a2a",
}
#: Markers that show an ontology body declares an access contract.
CONTRACT_MARKERS = (
    "https://knuckles-team.github.io/agent-connector-sdk/access#",
    "AccessContract",
)
_CATALOG: list[Any] = []


@dataclass(frozen=True, slots=True)
class SourceApprovalDecision:
    """The result of checking an EG source-approval receipt.

    This is a local projection of a producer decision.  Arbora never treats
    this record as authority: the EG foreign-read phases revalidate the
    durable approval and owner catalog before binding a source.
    """

    connector: str
    contract_iri: str
    mapping_iri: str
    outcome: Literal["approved", "held"]
    policy_id: str
    reason: str


_SOURCE_KIND_BY_ACCESS = {
    "mcp_tool": "mcp_tool",
    "http_endpoint": "http_endpoint",
    "graphql_query": "graphql_query",
    "a2a_skill": "a2a_skill",
}


def _mapping(value: Any) -> Mapping[str, Any] | None:
    """Read a generated Pydantic DTO without importing the engine package."""

    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        return None
    try:
        result = dump(mode="json")
    except (TypeError, ValueError):
        return None
    return result if isinstance(result, Mapping) else None


def _receipt_registration(
    receipt: Any,
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    body = _mapping(receipt)
    if body is None:
        return None
    registration = _mapping(body.get("registration"))
    return (body, registration) if registration is not None else None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def source_approval_decision(
    contract: Any,
    receipt: Any,
    *,
    connector: str,
    resource_uri: str,
    policy_id: str = "eg.connector-pack.source-approval",
) -> SourceApprovalDecision:
    """Project one *existing* EG approval receipt onto a parsed contract.

    ``receipt`` must be the generated ``ConnectorPackSourceApprovalResult``
    returned by EG after its current-head/body-digest CAS.  This helper does
    no write and accepts no local boolean as an approval substitute.  A
    missing, held or mismatched receipt is a held decision.
    """

    contract_iri = _text(getattr(contract, "contract_iri", ""))
    decision = SourceApprovalDecision(
        connector=connector,
        contract_iri=contract_iri,
        mapping_iri=contract_iri,
        outcome="held",
        policy_id=policy_id,
        reason="engine source approval receipt is absent or malformed",
    )
    parsed = _receipt_registration(receipt)
    if parsed is None:
        return decision
    body, registration = parsed
    approval = registration.get("approval")
    if hasattr(approval, "value"):
        approval = approval.value
    expected_kind = _SOURCE_KIND_BY_ACCESS.get(
        str(getattr(contract, "access_kind", ""))
    )
    expected = {
        "connector": connector,
        "contract_iri": contract_iri,
        "resource_uri": resource_uri,
        "source_id": _text(getattr(contract, "source_id", "")),
        "entity": _text(getattr(contract, "entity", "")),
        "class_iri": _text(getattr(contract, "class_iri", "")),
        "key_field": _text(getattr(contract, "key_field", "")),
        "operation": _text(getattr(contract, "operation", "")),
        "source_kind": expected_kind,
    }
    if (
        approval != "approved"
        or body.get("connector") != connector
        or not contract_iri
        or expected_kind is None
        or any(not value for key, value in expected.items() if key != "source_kind")
        or any(registration.get(key) != value for key, value in expected.items())
        or not isinstance(body.get("contract_digest"), str)
        or len(body["contract_digest"]) != 64
        or any(
            character not in "0123456789abcdef" for character in body["contract_digest"]
        )
        or not isinstance(body.get("tenant_id"), str)
        or not body["tenant_id"]
    ):
        return SourceApprovalDecision(
            connector=connector,
            contract_iri=contract_iri,
            mapping_iri=contract_iri,
            outcome="held",
            policy_id=policy_id,
            reason="engine source approval registration does not match the contract",
        )
    return SourceApprovalDecision(
        connector=connector,
        contract_iri=contract_iri,
        mapping_iri=contract_iri,
        outcome="approved",
        policy_id=policy_id,
        reason="EG durable source approval receipt accepted for binding",
    )


def approved_source_id(receipt: Any) -> str | None:
    """Return the EG registration name from a generated approval receipt."""

    parsed = _receipt_registration(receipt)
    if parsed is None:
        return None
    value = parsed[1].get("source_id")
    return value if isinstance(value, str) and value else None


def fleet_virtual_catalog() -> Any:
    """The process virtual catalog that fleet onboarding registers into."""

    if not _CATALOG:
        raise not_served("knowledge_graph.virtual_graph", "EG-UNIFIED-DATA-PLANE-R037")
    return _CATALOG[0]


def pack_ontology_resources(pack: Any) -> tuple[tuple[str, str], ...]:
    """The ``(uri, body)`` pair of every ``ontology://`` entry in a captured pack.

    Each body is read from the pack archive by its section (SDK 0.5).
    """

    archive = pack.archive
    return tuple(
        (str(entry.uri), entry_bytes(archive, entry).decode("utf-8"))
        for entry in archive.entries
        if str(entry.uri).startswith("ontology://")
    )


def pack_ontologies(pack: Any) -> tuple[str, ...]:
    """The body of every ``ontology://`` entry in a captured pack."""

    return tuple(body for _, body in pack_ontology_resources(pack))


def declares_contracts(text: str) -> bool:
    """True when an ontology body uses the access-contract vocabulary."""

    return any(marker in text for marker in CONTRACT_MARKERS)


def _parser() -> Any:
    try:
        from agent_connector_sdk.access_contract import parse_access_contracts
    except ImportError:
        logger.debug("connector SDK has no access_contract module; skipping")
        return None
    return parse_access_contracts


def parse_pack_contracts(pack: Any) -> tuple[Any, ...]:
    """Every access contract that a pack's ontologies declare."""

    parse = _parser()
    if parse is None:
        return ()
    found: list[Any] = []
    for uri, text in pack_ontology_resources(pack):
        if declares_contracts(text):
            found.extend(_parse_one(parse, text, uri))
    return tuple(found)


def _parse_one(parse: Any, text: str, uri: str) -> tuple[Any, ...]:
    """Parse one ontology body; log and skip a body the parser rejects."""

    try:
        return tuple(parse(text))
    except ValueError as exc:
        logger.warning(
            "skipping %s: access contracts unparsable: %s",
            uri,
            exc,
        )
        return ()


def _by_source(contracts: Sequence[Any]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for contract in contracts:
        grouped.setdefault(contract.source_id, []).append(contract)
    return grouped


def _entity(contract: Any) -> Any:
    """Translate one SDK contract into the AU discovery metadata shape.

    Registration records only the fields declared by the connector pack.  It
    does not infer fields from a live response, which keeps the catalog useful
    for later approval while ensuring an unapproved contract cannot serve a
    row accidentally.
    """

    from agent_utilities.knowledge_graph.virtual_graph import DiscoveredEntity

    name = getattr(contract, "entity", None)
    key = getattr(contract, "key_field", None)
    operation = getattr(contract, "operation", None)
    predicates = getattr(contract, "predicates", ())
    if not isinstance(name, str) or not name:
        raise ValueError("access contract has no stable entity, key or operation")
    if not isinstance(key, str) or not key:
        raise ValueError("access contract has no stable entity, key or operation")
    if not isinstance(operation, str) or not operation:
        raise ValueError("access contract has no stable entity, key or operation")

    fields: list[str] = []
    for binding in predicates:
        if isinstance(binding, (str, bytes)) or not isinstance(binding, Sequence):
            raise ValueError("access contract predicate is malformed")
        if len(binding) != 2:
            raise ValueError("access contract predicate is malformed")
        _property_iri, source_field = binding
        if not isinstance(source_field, str) or not source_field:
            raise ValueError("access contract predicate field is malformed")
        if source_field not in fields:
            fields.append(source_field)
    return DiscoveredEntity(
        name=name,
        key=key,
        fields=tuple(fields),
        operation=operation,
    )


async def _unwired_call(operation: str, arguments: Mapping[str, Any]) -> Any:
    """Keep the metadata adapter fail-closed until a governed reader binds it.

    The live source executor owns the authenticated ``fleet.call`` path.  The
    virtual-graph adapter is still registered here so selection and approval
    can reason over metadata, but a direct catalog fetch must never become an
    ungoverned connector call.
    """

    del arguments
    raise LookupError(f"no governed live binding for {operation!r}")


def _register_source(
    catalog: Any, connector: str, source_id: str, contracts: Sequence[Any]
) -> None:
    try:
        from agent_utilities.knowledge_graph.virtual_graph import (
            MetadataContract,
            OperationAdapter,
            SourceConnection,
        )
    except ImportError:
        raise not_served(
            "knowledge_graph.virtual_graph", "EG-UNIFIED-DATA-PLANE-R037"
        ) from None

    if not contracts:
        raise ValueError("a source needs at least one access contract")
    source_kinds: list[str] = []
    for contract in contracts:
        access_kind = getattr(contract, "access_kind", None)
        if not isinstance(access_kind, str):
            raise ValueError("access contracts for one source must share a known kind")
        source_kind = SOURCE_KIND_BY_ACCESS.get(access_kind)
        if source_kind is None:
            raise ValueError("access contracts for one source must share a known kind")
        source_kinds.append(source_kind)
    if len(set(source_kinds)) != 1:
        raise ValueError("access contracts for one source must share a known kind")
    kind = source_kinds[0]
    entities = tuple(_entity(contract) for contract in contracts)
    metadata = MetadataContract(
        source_id=source_id,
        schema_version="1",
        entities=entities,
    )
    connection = SourceConnection(
        source_id=source_id,
        kind=kind,
        endpoint_ref=f"fleet://{connector}",
    )
    adapter = OperationAdapter(connection, metadata, _unwired_call)
    catalog.register(connection, metadata, adapter)


def _add_mappings(catalog: Any, contracts: Sequence[Any]) -> int:
    try:
        from agent_utilities.knowledge_graph.virtual_graph import VirtualMapping
    except ImportError:
        raise not_served(
            "knowledge_graph.virtual_graph", "EG-UNIFIED-DATA-PLANE-R037"
        ) from None

    existing = {mapping.mapping_id: mapping for mapping in catalog.mappings}
    added = 0
    for contract in contracts:
        fields = dict(contract.virtual_mapping_fields())
        # A pack declaration is evidence for a candidate mapping only.  The
        # approval decision belongs to the governed EG source-registration
        # path and must never be smuggled in through connector metadata.
        fields["approved"] = False
        mapping = VirtualMapping(**fields)
        previous = existing.get(mapping.mapping_id)
        if previous is not None:
            # A renewal must not turn an already EG-approved projection back
            # into a held one.  Conversely, changed contract fields replace
            # the old row with a held candidate so stale data can never keep
            # serving after a connector release.
            same_shape = all(
                getattr(previous, name) == getattr(mapping, name)
                for name in (
                    "mapping_id",
                    "source_id",
                    "entity",
                    "class_iri",
                    "key_field",
                    "predicates",
                    "version",
                )
            )
            if same_shape:
                continue
            index = next(
                index
                for index, item in enumerate(catalog.mappings)
                if item.mapping_id == mapping.mapping_id
            )
            catalog.mappings[index] = mapping
            existing[mapping.mapping_id] = mapping
            added += 1
            continue
        catalog.add_mapping(mapping)
        existing[mapping.mapping_id] = mapping
        added += 1
    return added


def bind_approved_mapping(
    catalog: Any,
    contract: Any,
    receipt: Any,
    *,
    connector: str,
    resource_uri: str,
    policy_id: str = "eg.connector-pack.source-approval",
) -> SourceApprovalDecision:
    """Apply an EG-approved receipt to one existing catalog candidate.

    The local ``approved`` bit is only a selection projection.  The foreign
    read adapter still sends the EG receipt, and EG rechecks its durable
    approval, owner catalog and current source registration on every phase.
    No receipt means no local approval and no source read.
    """

    decision = source_approval_decision(
        contract,
        receipt,
        connector=connector,
        resource_uri=resource_uri,
        policy_id=policy_id,
    )
    if decision.outcome != "approved":
        return decision
    mapping_id = str(getattr(contract, "contract_iri", ""))
    for index, mapping in enumerate(catalog.mappings):
        if mapping.mapping_id != mapping_id:
            continue
        fields = dict(contract.virtual_mapping_fields())
        candidate = type(mapping)(**fields, approved=False)
        if any(
            getattr(mapping, name) != getattr(candidate, name)
            for name in (
                "mapping_id",
                "source_id",
                "entity",
                "class_iri",
                "key_field",
                "predicates",
                "version",
            )
        ):
            return SourceApprovalDecision(
                connector=connector,
                contract_iri=mapping_id,
                mapping_iri=mapping_id,
                outcome="held",
                policy_id=policy_id,
                reason="catalog mapping does not match the approved contract",
            )
        catalog.mappings[index] = replace(mapping, approved=True)
        return decision
    return SourceApprovalDecision(
        connector=connector,
        contract_iri=mapping_id,
        mapping_iri=mapping_id,
        outcome="held",
        policy_id=policy_id,
        reason="approved contract has no registered catalog mapping",
    )


def register_access_contracts(pack: Any, *, connector: str, catalog: Any = None) -> int:
    """Register a pack's access contracts unapproved; return mappings added."""

    contracts = parse_pack_contracts(pack)
    if not contracts:
        return 0
    target = catalog if catalog is not None else fleet_virtual_catalog()
    added = 0
    for source_id, items in _by_source(contracts).items():
        _register_source(target, connector, source_id, items)
        added += _add_mappings(target, items)
    return added


__all__ = [
    "CONTRACT_MARKERS",
    "SourceApprovalDecision",
    "SOURCE_KIND_BY_ACCESS",
    "approved_source_id",
    "bind_approved_mapping",
    "declares_contracts",
    "fleet_virtual_catalog",
    "pack_ontologies",
    "pack_ontology_resources",
    "parse_pack_contracts",
    "register_access_contracts",
    "source_approval_decision",
]
