"""Engine-held federation source ownership and grants (ARBORA-OPS-R019.1/.2).

Medulla owns the facts.  The ``federation_sources`` port keeps no state of its
own: each registered source is a typed ``FederationSource`` node on the
control graph, its owner a node property, and every grant a ``GRANTED_TO``
edge from the source node to a ``FederationPrincipal`` node.  Reads go back
to the engine every time, so a revoked grant is gone on the next call.  The
endpoint configuration (and its credentials) is handed to the engine's own
``RegisterForeignSource`` and never copied into the ownership record.

The control graph is not reachable through the caller-bound generic graph
operations, so a caller cannot forge an ownership node or a grant edge.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import msgpack

from arbora.api.invoke.pipeline import OperationRefused

__all__ = ["EngineFederationSources", "principal_node_id", "source_node_id"]

ClientFactory = Callable[[str], Awaitable[Any]]

SOURCE_LABEL = "FederationSource"
PRINCIPAL_LABEL = "FederationPrincipal"
GRANT_LABEL = "GRANTED_TO"


def source_node_id(tenant: str, source_id: str) -> str:
    return f"federation-source:{tenant}:{source_id}"


def principal_node_id(tenant: str, principal: str) -> str:
    return f"federation-principal:{tenant}:{principal}"


def _decode(payload: Any) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, bytes | bytearray | memoryview):
        decoded = msgpack.unpackb(bytes(payload), raw=False)
        return decoded if isinstance(decoded, dict) else None
    return None


def _pack(body: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(body), use_bin_type=True)


def _node_id(row: Any) -> str | None:
    """One ``GetNodesByLabel`` row is ``(id, properties)``."""
    if isinstance(row, list | tuple) and len(row) == 2 and row[0]:
        return str(row[0])
    return None


class EngineFederationSources:
    """The ``federation_sources`` port over the engine's generic graph methods."""

    def __init__(
        self,
        control_client: ClientFactory,
        data_client: ClientFactory,
        *,
        control_graph: str,
        data_graph: str,
        page_size: int = 200,
    ) -> None:
        self._control = control_client
        self._data = data_client
        self._control_graph = control_graph
        self._data_graph = data_graph
        self._page = page_size

    async def _send(
        self, tenant: str, method: str, params: dict[str, Any], *, key: str | None
    ) -> Any:
        from epistemic_graph.generated import send_by_id

        client = await self._control(tenant)
        return await send_by_id(
            client, method, params, self._control_graph, idempotency_key=key
        )

    async def _properties(self, tenant: str, node_id: str) -> dict[str, Any] | None:
        raw = await self._send(
            tenant, "GetNodeProperties", {"node_id": node_id}, key=None
        )
        return _decode(raw)

    async def _grantees(self, tenant: str, source_id: str) -> list[str]:
        prefix = principal_node_id(tenant, "")
        neighbours = await self._send(
            tenant,
            "GetNeighbors",
            {"node_id": source_node_id(tenant, source_id)},
            key=None,
        )
        return sorted(
            str(node)[len(prefix) :]
            for node in neighbours or ()
            if str(node).startswith(prefix)
        )

    async def _record(
        self, tenant: str, source_id: str, props: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "source_id": source_id,
            "owner": props.get("owner"),
            "grantees": await self._grantees(tenant, source_id),
        }

    async def get(self, source_id: str, *, tenant: str) -> dict[str, Any] | None:
        props = await self._properties(tenant, source_node_id(tenant, source_id))
        if not props or props.get("tenant") != tenant:
            return None
        return await self._record(tenant, source_id, props)

    async def list(self, *, tenant: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        after: str | None = None
        prefix = source_node_id(tenant, "")
        while True:
            page = await self._send(
                tenant,
                "GetNodesByLabel",
                {"label": SOURCE_LABEL, "limit": self._page, "after": after},
                key=None,
            )
            ids = [i for i in (_node_id(n) for n in page or ()) if i]
            for node_id in ids:
                if node_id.startswith(prefix):
                    record = await self.get(node_id[len(prefix) :], tenant=tenant)
                    if record is not None:
                        records.append(record)
            if len(ids) < self._page:
                return records
            after = ids[-1]

    async def register(
        self,
        source_id: str,
        source: Mapping[str, Any],
        *,
        tenant: str,
        owner: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        node_id = source_node_id(tenant, source_id)
        existing = await self._properties(tenant, node_id)
        if existing is None:
            await self._send(
                tenant,
                "CreateNodeIfAbsent",
                {
                    "node_id": node_id,
                    "properties_msgpack": _pack(
                        {
                            "type": SOURCE_LABEL,
                            "label": SOURCE_LABEL,
                            "tenant": tenant,
                            "source_id": source_id,
                            "owner": owner,
                        }
                    ),
                },
                key=f"{idempotency_key}:owner",
            )
            existing = await self._properties(tenant, node_id)
        if not existing or existing.get("owner") != owner:
            raise OperationRefused(
                "PERMISSION_DENIED", {"reason": "source is owned by another principal"}
            )
        from epistemic_graph.generated import send_by_id

        client = await self._data(tenant)
        await send_by_id(
            client,
            "RegisterForeignSource",
            {"name": source_id, "source": dict(source)},
            self._data_graph,
            idempotency_key=f"{idempotency_key}:engine",
        )
        return await self._record(tenant, source_id, existing)

    async def probe(self, source_id: str, *, tenant: str) -> dict[str, Any]:
        from epistemic_graph.generated import send_by_id

        client = await self._data(tenant)
        try:
            await send_by_id(
                client,
                "QueryForeignColumns",
                {"name": source_id, "columns": [], "predicates": []},
                self._data_graph,
                idempotency_key=None,
            )
        except Exception as exc:  # noqa: BLE001 -- a probe reports, never raises
            return {
                "source_id": source_id,
                "reachable": False,
                "error": type(exc).__name__,
            }
        return {"source_id": source_id, "reachable": True}

    async def grant(
        self, source_id: str, grantee: str, *, tenant: str, idempotency_key: str
    ) -> dict[str, Any]:
        principal = principal_node_id(tenant, grantee)
        await self._send(
            tenant,
            "CreateNodeIfAbsent",
            {
                "node_id": principal,
                "properties_msgpack": _pack(
                    {
                        "type": PRINCIPAL_LABEL,
                        "label": PRINCIPAL_LABEL,
                        "tenant": tenant,
                        "principal": grantee,
                    }
                ),
            },
            key=f"{idempotency_key}:principal",
        )
        await self._send(
            tenant,
            "AddEdge",
            {
                "source_id": source_node_id(tenant, source_id),
                "target_id": principal,
                "properties_msgpack": _pack({"type": GRANT_LABEL, "tenant": tenant}),
            },
            key=f"{idempotency_key}:grant",
        )
        return await self._require(source_id, tenant=tenant)

    async def revoke(
        self, source_id: str, grantee: str, *, tenant: str, idempotency_key: str
    ) -> dict[str, Any]:
        await self._send(
            tenant,
            "RemoveEdge",
            {
                "source_id": source_node_id(tenant, source_id),
                "target_id": principal_node_id(tenant, grantee),
            },
            key=f"{idempotency_key}:revoke",
        )
        return await self._require(source_id, tenant=tenant)

    async def _require(self, source_id: str, *, tenant: str) -> dict[str, Any]:
        record = await self.get(source_id, tenant=tenant)
        if record is None:
            raise LookupError("federation source not found")
        return record
