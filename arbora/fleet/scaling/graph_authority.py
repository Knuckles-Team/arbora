"""The scale authority inventory, read from the graph (ARBORA-FLEET-R024.15).

The graph is the authority: failure domains, resource pools, workload classes, scale
units and controller registrations are typed graph nodes, one node per record, whose
properties are the record's own fields. Arbora stores none of them. A snapshot is read
through the engine's existing ``GetNodesByLabel`` as the verified caller and validated
whole by ``ScaleAuthority``; a node that does not parse refuses the snapshot, so a
partial inventory never admits an intent.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import engine_send, node_rows
from arbora.fleet.scaling.scaling_authority import (
    ControllerRegistration,
    FailureDomain,
    ResourcePool,
    ScaleAuthority,
    ScaleUnit,
    WorkloadClass,
)

#: The graph class of each inventory record, in the field order of ``ScaleAuthority``.
INVENTORY: tuple[tuple[str, str, type[BaseModel]], ...] = (
    ("failure_domains", "ScaleFailureDomain", FailureDomain),
    ("pools", "ScaleResourcePool", ResourcePool),
    ("workloads", "ScaleWorkloadClass", WorkloadClass),
    ("units", "ScaleUnit", ScaleUnit),
    ("controllers", "ScaleControllerRegistration", ControllerRegistration),
)
#: The one node that carries the inventory's revision.
REVISION_LABEL = "ScaleAuthorityRevision"
REVISION_FIELD = "authority_revision"

#: Properties the engine adds to a node; they are not record fields.
SYSTEM_PROPERTIES = frozenset({"type", "labels", "tenant_id", "graph", "id"})

PAGE = 500
MAX_PAGES = 100


def _unavailable(reason: str) -> OperationRefused:
    return OperationRefused("UNAVAILABLE", {"reason": reason})


async def class_nodes(context: Any, label: str) -> list[tuple[str, dict[str, Any]]]:
    """Every node of one class, paged by the last node id."""
    found: list[tuple[str, dict[str, Any]]] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        request: dict[str, Any] = {"label": label, "limit": PAGE}
        if after is not None:
            request["after"] = after
        page = node_rows(await engine_send(context, "GetNodesByLabel", request))
        found.extend(page)
        if len(page) < PAGE:
            return found
        after = page[-1][0]
    raise _unavailable(f"scale authority class {label} exceeds the page bound")


def _record(model: type[BaseModel], node: str, props: Mapping[str, Any]) -> Any:
    fields = {k: v for k, v in props.items() if k not in SYSTEM_PROPERTIES}
    try:
        return model.model_validate(fields)
    except ValidationError as invalid:
        raise _unavailable(
            f"scale authority node {node} is not a valid record"
        ) from invalid


async def _revision(context: Any) -> int:
    nodes = await class_nodes(context, REVISION_LABEL)
    if len(nodes) != 1 or not isinstance(nodes[0][1].get(REVISION_FIELD), int):
        raise _unavailable("the graph holds no single scale authority revision")
    return int(nodes[0][1][REVISION_FIELD])


class GraphScaleAuthority:
    """Compose the ``ScaleAuthority`` snapshot of the verified caller's graph."""

    async def snapshot(self, context: Any) -> ScaleAuthority:
        revision = await _revision(context)
        records: dict[str, tuple[Any, ...]] = {}
        for field, label, model in INVENTORY:
            nodes = await class_nodes(context, label)
            records[field] = tuple(_record(model, node, props) for node, props in nodes)
        try:
            return ScaleAuthority(authority_revision=revision, **records)
        except ValidationError as invalid:
            raise _unavailable(
                "the scale authority inventory is inconsistent"
            ) from invalid


__all__ = [
    "INVENTORY",
    "REVISION_FIELD",
    "REVISION_LABEL",
    "SYSTEM_PROPERTIES",
    "GraphScaleAuthority",
    "class_nodes",
]
