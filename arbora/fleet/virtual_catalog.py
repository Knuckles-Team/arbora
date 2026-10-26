"""Arbora's process-local source-contract metadata projection.

This module owns the small in-process registry used while a connector pack is
being admitted.  It deliberately contains no graph store, ontology reasoner,
federator, or approval authority.  EG owns durable source registration and
approval; the governed source reader owns live calls.  The local ``approved``
flag is only a projection of an EG receipt and cannot make a read callable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_SOURCE_KINDS = frozenset({"api", "mcp", "a2a", "graphql"})
_CREDENTIAL_HINTS = ("@", "password=", "token=", "secret=")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class SourceConnection:
    """A source metadata reference without credentials or live transport."""

    source_id: str
    kind: str
    endpoint_ref: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(bool(self.source_id), "a source connection needs a source_id")
        _require(self.kind in _SOURCE_KINDS, f"unknown source kind {self.kind!r}")
        _require(
            "://" in self.endpoint_ref,
            "endpoint_ref must be a non-secret URI reference",
        )
        lowered = self.endpoint_ref.lower()
        _require(
            not any(hint in lowered for hint in _CREDENTIAL_HINTS),
            "endpoint_ref must not carry credentials",
        )

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True, slots=True)
class DiscoveredEntity:
    """One entity named by an admitted contract."""

    name: str
    key: str
    fields: tuple[str, ...]
    operation: str


@dataclass(frozen=True, slots=True)
class MetadataContract:
    """The connector-declared metadata needed for candidate validation."""

    source_id: str
    schema_version: str
    entities: tuple[DiscoveredEntity, ...]

    def entity(self, name: str) -> DiscoveredEntity | None:
        return next((item for item in self.entities if item.name == name), None)


@dataclass(frozen=True, slots=True)
class VirtualMapping:
    """A candidate class-to-source mapping, held until EG approval."""

    mapping_id: str
    source_id: str
    entity: str
    class_iri: str
    key_field: str
    predicates: tuple[tuple[str, str], ...] = ()
    approved: bool = False
    version: str = "1"

    def field_for(self, predicate: str) -> str | None:
        return dict(self.predicates).get(predicate)


def check_mapping(mapping: VirtualMapping, contract: MetadataContract) -> None:
    """Reject mappings that name metadata the connector never declared."""

    _require(mapping.source_id == contract.source_id, "mapping/source mismatch")
    entity = contract.entity(mapping.entity)
    _require(entity is not None, f"undiscovered entity {mapping.entity!r}")
    known = {entity.key, *entity.fields} if entity is not None else set()
    used = {mapping.key_field, *(field for _, field in mapping.predicates)}
    unknown = sorted(used - known)
    _require(not unknown, f"undiscovered fields {unknown}")


@dataclass(slots=True)
class OperationAdapter:
    """Metadata adapter whose call path remains governed and fail-closed."""

    connection: SourceConnection
    metadata: MetadataContract
    call: Callable[[str, Mapping[str, Any]], Awaitable[Any]]

    async def fetch(
        self,
        mapping: VirtualMapping,
        field: str | None = None,
        values: Sequence[Any] | None = None,
    ) -> Any:
        if mapping.source_id != self.connection.source_id:
            raise LookupError("mapping is not registered for this source")
        entity = self.metadata.entity(mapping.entity)
        if entity is None:
            raise LookupError(f"mapping entity {mapping.entity!r} is not registered")
        arguments: dict[str, Any] = {}
        if field is not None:
            arguments["field"] = field
        if values is not None:
            arguments["values"] = tuple(values)
        return await self.call(entity.operation, arguments)


@dataclass(slots=True)
class VirtualCatalog:
    """The one Arbora process registry for source-contract projections."""

    connections: dict[str, SourceConnection] = field(default_factory=dict)
    contracts: dict[str, MetadataContract] = field(default_factory=dict)
    adapters: dict[str, Any] = field(default_factory=dict)
    mappings: list[VirtualMapping] = field(default_factory=list)

    def register(
        self,
        connection: SourceConnection,
        contract: MetadataContract,
        adapter: OperationAdapter,
    ) -> None:
        if contract.source_id != connection.source_id:
            raise ValueError("contract and connection name different sources")
        self.connections[connection.source_id] = connection
        self.contracts[connection.source_id] = contract
        self.adapters[connection.source_id] = adapter

    def add_mapping(self, mapping: VirtualMapping) -> None:
        contract = self.contracts.get(mapping.source_id)
        if contract is None:
            raise ValueError(f"unregistered source {mapping.source_id!r}")
        check_mapping(mapping, contract)
        self.mappings.append(mapping)

    def mapping_for(
        self, class_iri: str, _ontology: Any | None = None
    ) -> VirtualMapping | None:
        """Return an approved exact mapping; ontology inference stays in EG."""

        return next(
            (
                mapping
                for mapping in self.mappings
                if mapping.approved and mapping.class_iri == class_iri
            ),
            None,
        )


__all__ = [
    "DiscoveredEntity",
    "MetadataContract",
    "OperationAdapter",
    "SourceConnection",
    "VirtualCatalog",
    "VirtualMapping",
    "check_mapping",
]
