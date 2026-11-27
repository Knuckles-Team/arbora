"""The scale inventory producer: one operator write of the typed graph nodes (ARBORA-FLEET-R024.17).

The inventory is graph truth that an operator states; Arbora keeps none of it. One call
sets the whole inventory at the next revision: it reads the current nodes as the verified
caller, refuses a stale base revision, validates the proposed inventory whole through
``ScaleAuthority``, then writes one node per record through the engine's node writes
(add an absent node, compare-and-set a changed one, remove a node the inventory no longer
names) and moves the one ``ScaleAuthorityRevision`` node last. A reader that meets the
inventory part-written refuses it as inconsistent, so a partial inventory never admits an
intent. Every write is keyed by the request's idempotency key and its node, so a replay
repeats no effect.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

import msgpack
from pydantic import BaseModel, ValidationError

from arbora.api.domain.fleet.scaling.graph_authority import (
    INVENTORY,
    REVISION_FIELD,
    REVISION_LABEL,
    SYSTEM_PROPERTIES,
    class_nodes,
)
from arbora.api.domain.fleet.scaling.scaling_authority import ScaleAuthority
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import decode, send_on_client
from arbora.core.claims import claim_values
from arbora.security.anonymous import client_scope

#: The one node that carries the inventory's revision.
REVISION_NODE = "scale:authority-revision"

#: The identity of each record, per inventory field.
_KEYS: dict[str, Callable[[Any], str]] = {
    "failure_domains": lambda record: record.domain_id,
    "pools": lambda record: record.pool_id,
    "workloads": lambda record: record.workload_id,
    "units": lambda record: record.unit_id,
    "controllers": lambda record: f"{record.unit_id}:{record.controller_id}",
}


def node_id(label: str, key: str) -> str:
    return f"scale:{label}:{key}"


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


def desired_nodes(
    records: Mapping[str, tuple[BaseModel, ...]],
) -> dict[str, tuple[str, dict[str, Any]]]:
    """``node id -> (label, properties)`` of the proposed inventory."""
    nodes: dict[str, tuple[str, dict[str, Any]]] = {}
    for field, label, _model in INVENTORY:
        for record in records.get(field, ()):
            nid = node_id(label, _KEYS[field](record))
            if nid in nodes:
                raise OperationRefused("INVALID_ARGUMENT", {"duplicate": nid})
            nodes[nid] = (label, record.model_dump(mode="json"))
    return nodes


async def current_revision(context: Any) -> int:
    """The inventory's revision, ``0`` when none was ever set."""
    nodes = await class_nodes(context, REVISION_LABEL)
    if not nodes:
        return 0
    value = nodes[0][1].get(REVISION_FIELD)
    if len(nodes) != 1 or not isinstance(value, int):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the graph holds no single scale revision"}
        )
    return value


class _Writer:
    """Engine node writes as the verified caller, each keyed by request and node."""

    def __init__(self, context: Any, graph: str) -> None:
        self.context, self.graph = context, graph

    async def send(self, method: str, node: str, params: dict[str, Any]) -> Any:
        digest = hashlib.sha256(f"{method}:{node}".encode()).hexdigest()[:16]
        key = f"{self.context.idempotency_key}:{digest}"
        context = self.context
        with client_scope(context.client, claim_values(context.caller.engine_claims)):
            return decode(
                await send_on_client(
                    context.client,
                    method,
                    {"node_id": node, **params},
                    self.graph,
                    key=key,
                )
            )

    async def add(self, node: str, label: str, props: Mapping[str, Any]) -> None:
        body = _pack({"labels": [label], **props})
        await self.send("AddNode", node, {"properties_msgpack": body})

    async def remove(self, node: str) -> None:
        await self.send("RemoveNode", node, {})

    async def swap(
        self, node: str, conditions: Mapping[str, Any], updates: Mapping[str, Any]
    ) -> bool:
        swapped = await self.send(
            "CompareAndSetNodeFields",
            node,
            {
                "conditions_msgpack": _pack(conditions),
                "updates_msgpack": _pack(updates),
            },
        )
        return swapped is not False


async def _existing(context: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for _field, label, _model in INVENTORY:
        for nid, props in await class_nodes(context, label):
            fields = {k: v for k, v in props.items() if k not in SYSTEM_PROPERTIES}
            found[nid] = (label, fields)
    return found


async def _change_node(
    writer: _Writer,
    node: str,
    label: str,
    props: dict[str, Any],
    before: dict[str, Any],
) -> None:
    """Move one existing node to ``props``: compare-and-set, or replace when a field goes."""
    if not set(before) <= set(props):
        await writer.remove(node)
        await writer.add(node, label, props)
        return
    changed = {k: v for k, v in props.items() if before.get(k) != v}
    conditions = {k: before[k] for k in changed if k in before}
    if not await writer.swap(node, conditions, changed):
        raise OperationRefused("REVISION_CONFLICT", {"node": node})


async def _write_node(
    writer: _Writer,
    node: str,
    label: str,
    props: dict[str, Any],
    old: tuple[str, dict[str, Any]] | None,
) -> str:
    """Write one node; ``added``, ``changed`` or ``unchanged``."""
    if old is None:
        await writer.add(node, label, props)
        return "added"
    if old[1] == props:
        return "unchanged"
    await _change_node(writer, node, label, props, old[1])
    return "changed"


async def _move_revision(writer: _Writer, current: int, revision: int) -> None:
    if current == 0:
        await writer.add(REVISION_NODE, REVISION_LABEL, {REVISION_FIELD: revision})
    elif not await writer.swap(
        REVISION_NODE, {REVISION_FIELD: current}, {REVISION_FIELD: revision}
    ):
        raise OperationRefused("REVISION_CONFLICT", {"node": REVISION_NODE})


async def set_inventory(
    context: Any, base_revision: int, records: Mapping[str, tuple[BaseModel, ...]]
) -> dict[str, int]:
    """Set the inventory to ``records`` at revision ``base_revision + 1``."""
    current = await current_revision(context)
    if current != base_revision:
        raise OperationRefused(
            "REVISION_CONFLICT", {"current_revision": current, "base": base_revision}
        )
    revision = base_revision + 1
    try:
        ScaleAuthority(authority_revision=revision, **records)
    except ValidationError as invalid:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"reason": str(invalid)[:512]}
        ) from invalid
    desired = desired_nodes(records)
    existing = await _existing(context)
    writer = _Writer(context, await caller_dispatch_graph(context))
    tally = {"added": 0, "changed": 0, "unchanged": 0, "removed": 0}
    for node in sorted(desired):
        label, props = desired[node]
        tally[await _write_node(writer, node, label, props, existing.get(node))] += 1
    for node in sorted(set(existing) - set(desired)):
        await writer.remove(node)
        tally["removed"] += 1
    await _move_revision(writer, current, revision)
    return {"authority_revision": revision, **tally}


__all__ = [
    "REVISION_NODE",
    "current_revision",
    "desired_nodes",
    "node_id",
    "set_inventory",
]
