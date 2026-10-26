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

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from arbora.api.errors import not_served
from arbora.fleet.declarations import entry_bytes
from arbora.fleet.virtual_catalog import (
    DiscoveredEntity,
    MetadataContract,
    OperationAdapter,
    SourceConnection,
    VirtualCatalog,
    VirtualMapping,
)

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

_CREDENTIAL_REFERENCE_PREFIXES = ("env://", "openbao://")


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
    #: The exact source-contract body digest, when one was available.
    contract_digest: str | None = None
    #: The native EG receipt projected by :func:`bind_approved_mapping`.
    receipt: Any = None


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
    approval_raw = registration.get("approval")
    approval = approval_raw
    if approval_raw is not None:
        approval = getattr(approval_raw, "value", approval_raw)
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


def bootstrap_fleet_virtual_catalog(catalog: Any = None) -> Any:
    """Install the one process-owned Arbora ``VirtualCatalog``.

    The catalog is a metadata/selection projection; it is not an authority.
    New mappings are held until an EG approval receipt is projected through
    :func:`bind_approved_mapping`.  Keeping the process singleton explicit
    prevents onboarding from silently creating a second, disconnected catalog
    and makes a tenant-mixing replacement fail closed.
    """

    if catalog is None:
        catalog = VirtualCatalog()
    if _CATALOG:
        if _CATALOG[0] is not catalog:
            raise RuntimeError("fleet virtual catalog is already bound")
        return _CATALOG[0]
    _CATALOG.append(catalog)
    return catalog


def _credential_reference(value: Any) -> bool:
    return isinstance(value, str) and any(
        value.startswith(prefix) and len(value) > len(prefix)
        for prefix in _CREDENTIAL_REFERENCE_PREFIXES
    )


def _explicit_read_only(contract: Any) -> bool | None:
    for name in ("read_only", "read_only_hint"):
        value = getattr(contract, name, None)
        if value is not None:
            return value is True
    annotations = getattr(contract, "annotations", None)
    if isinstance(annotations, Mapping):
        for name in ("readOnlyHint", "read_only_hint", "read_only"):
            if name in annotations:
                return annotations[name] is True
    effect = getattr(contract, "effect", None)
    if effect is not None:
        value = getattr(effect, "value", effect)
        if isinstance(value, str):
            return value.lower() in {"read", "readonly", "read_only"}
    return None


def source_approval_policy(
    contract: Any,
    *,
    connector: str,
    admitted: bool,
    policy_id: str = "arbora.fleet.source-approval.read-only-admitted.v1",
) -> SourceApprovalDecision:
    """Evaluate the local admission policy without manufacturing authority.

    A positive result is only permission to ask EG's owner-scoped
    ``ConnectorPack.approve_source`` operation for a receipt.  It never sets a
    catalog mapping's ``approved`` bit.  The native receipt remains the sole
    source of read authority.
    """

    contract_iri = _text(getattr(contract, "contract_iri", ""))

    def held(reason: str) -> SourceApprovalDecision:
        return SourceApprovalDecision(
            connector=connector,
            contract_iri=contract_iri,
            mapping_iri=contract_iri,
            outcome="held",
            policy_id=policy_id,
            reason=reason,
        )

    if not admitted:
        return held("connector is not admitted by the fleet registry")
    if not contract_iri:
        return held("contract has no stable identity")
    access_kind = getattr(contract, "access_kind", None)
    if access_kind not in _SOURCE_KIND_BY_ACCESS:
        return held("access kind is not an approved read source kind")
    explicit = _explicit_read_only(contract)
    if explicit is False:
        return held("source operation is not declared read-only")
    if explicit is None:
        # Operation names are connector-owned strings and cannot establish
        # effect semantics.  An unknown operation is held until the producer
        # supplies its typed read-only fact; guessing from verbs such as
        # ``list`` or ``get`` would make a mutator readable by accident.
        return held("source operation effect is not declared read-only")
    if not _credential_reference(getattr(contract, "auth_ref", None)):
        return held(
            "source requires a credential reference, never a literal credential"
        )
    return SourceApprovalDecision(
        connector=connector,
        contract_iri=contract_iri,
        mapping_iri=contract_iri,
        outcome="approved",
        policy_id=policy_id,
        reason="admitted read-only contract may request native EG approval",
    )


def pack_contract_resources(pack: Any) -> tuple[tuple[Any, str, str], ...]:
    """Return ``(contract, ontology URI, body SHA-256)`` tuples from a pack.

    The digest is computed over the captured ontology body.  It is a selector
    for EG's current-head CAS, not a local approval or a substitute for the
    receipt returned by EG.
    """

    parse = _parser()
    if parse is None:
        return ()
    found: list[tuple[Any, str, str]] = []
    for uri, text in pack_ontology_resources(pack):
        if not declares_contracts(text):
            continue
        for contract in _parse_one(parse, text, uri):
            found.append(
                (contract, uri, hashlib.sha256(text.encode("utf-8")).hexdigest())
            )
    return tuple(found)


def fleet_virtual_catalog() -> Any:
    """The process virtual catalog that fleet onboarding registers into."""

    if not _CATALOG:
        raise not_served("arbora.fleet.virtual_catalog", "EG-UNIFIED-DATA-PLANE-R037")
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
    """Translate one SDK contract into Arbora discovery metadata.

    Registration records only the fields declared by the connector pack.  It
    does not infer fields from a live response, which keeps the catalog useful
    for later approval while ensuring an unapproved contract cannot serve a
    row accidentally.
    """

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
            invalidate = getattr(catalog, "invalidate_binding", None)
            if callable(invalidate):
                invalidate(mapping.mapping_id)
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
    scope: tuple[str, str] | None = None,
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
        approved_mapping = replace(mapping, approved=True)
        catalog.mappings[index] = approved_mapping
        record = getattr(catalog, "record_approved_source", None)
        if callable(record):
            try:
                record(
                    approved_mapping,
                    contract,
                    receipt,
                    connector=connector,
                    resource_uri=resource_uri,
                    scope=scope,
                )
            except (LookupError, ValueError) as exc:
                catalog.mappings[index] = mapping
                return SourceApprovalDecision(
                    connector=connector,
                    contract_iri=mapping_id,
                    mapping_iri=mapping_id,
                    outcome="held",
                    policy_id=policy_id,
                    reason=f"catalog source binding refused: {exc}",
                )
        return decision
    return SourceApprovalDecision(
        connector=connector,
        contract_iri=mapping_id,
        mapping_iri=mapping_id,
        outcome="held",
        policy_id=policy_id,
        reason="approved contract has no registered catalog mapping",
    )


def register_access_contracts(
    pack: Any,
    *,
    connector: str,
    catalog: Any = None,
    admitted: bool = False,
    decisions: list[SourceApprovalDecision] | None = None,
) -> int:
    """Register a pack's contracts as candidates; return mappings added.

    ``admitted`` is only the local fleet-registry input to the policy.  Even an
    ``approved`` policy decision remains held in the virtual catalog until a
    native EG approval receipt is supplied to :func:`bind_approved_mapping`.
    ``decisions`` is an optional evidence sink for the onboarding report.
    """

    contracts = parse_pack_contracts(pack)
    if not contracts:
        return 0
    if decisions is not None:
        decisions.extend(
            source_approval_policy(contract, connector=connector, admitted=admitted)
            for contract in contracts
        )
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
    "pack_contract_resources",
    "parse_pack_contracts",
    "register_access_contracts",
    "bootstrap_fleet_virtual_catalog",
    "source_approval_policy",
    "source_approval_decision",
]
