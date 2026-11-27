"""The engine-backed ports of :class:`AdmissionRun` (ARBORA-DATA-MARKET-R004).

Arbora holds no admission state. A run is one graph node and the tenant's one
active slot is a second node whose ``holder`` field is swapped from empty to the
run id, so the second claim loses. Approval is the engine ``GovernedChange``.
Probes go through the federation source probe; attach, detach and parity use
engine methods that ``main`` has not published, so a pinned release refuses
``UNAVAILABLE`` naming the method (PIN-LAG, see ``OVERSTORY-GAPS``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import msgpack
import yaml

from arbora.api.errors import EngineRefusal
from arbora.control_plane.admission.run import SOURCE_AUTHORITY_KIND

__all__ = [
    "EngineAdmissionGovernance",
    "EngineAdmissionSources",
    "EngineAdmissionStore",
    "PARITY_SAMPLE_DEFAULT",
    "active_node_id",
    "parity_sample",
]

Send = Callable[[str, dict[str, Any]], Awaitable[Any]]
PARITY_SAMPLE_DEFAULT = 1000
_SAMPLE_RANGE = (10, 100_000)
_PROPOSAL_TTL_MS = 7 * 24 * 3_600_000
_WINDOW_MS = 24 * 3_600_000
#: Engine methods ``main`` publishes that the pinned release lacks (MEDULLA-UNIFIED-DATA-PLANE
#: R769 and R770); each refuses ``UNAVAILABLE`` until pinned. Attach is the existing registry read.
SOURCE_METHODS = ("ForeignSourceDetach", "ForeignSourceParity")


def registered_name(source_id: str, table: str) -> str:
    """The registered source of one table: ``<name>.<table>``, or ``<name>`` when equal."""
    return source_id if table == source_id else f"{source_id}.{table}"


def parity_sample(document: Mapping[str, Any] | str | None = None) -> int:
    """``data_apps.parity_sample`` of ``data_apps.yml`` (default 1000, within [10, 100000])."""
    if document is None:
        from arbora.core.settings import platform_config_file

        path = platform_config_file("data_apps.yml")
        document = path.read_text() if path.is_file() else None
    parsed = yaml.safe_load(document) if isinstance(document, str) else document
    value = ((parsed or {}).get("data_apps") or {}).get("parity_sample")
    if value is None:
        return PARITY_SAMPLE_DEFAULT
    low, high = _SAMPLE_RANGE
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(
            f"data_apps.parity_sample must be an integer in [{low}, {high}]"
        )
    return value


def active_node_id(tenant: str) -> str:
    return f"admission-active:{tenant}"


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


def _props(blob: Any) -> dict[str, Any]:
    if isinstance(blob, Mapping):
        return dict(blob)
    if isinstance(blob, (bytes, bytearray, memoryview)):
        loaded = msgpack.unpackb(bytes(blob), raw=False)
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _took(answer: Any) -> bool:
    """A compare-and-set answer: only an explicit refusal to swap is a loss."""
    if isinstance(answer, Mapping):
        answer = answer.get("swapped", answer.get("updated", True))
    return answer is not False


class EngineAdmissionStore:
    """Run and active-slot nodes through one verified caller's ``send``."""

    def __init__(self, send: Send) -> None:
        self._send = send

    async def _node(self, node_id: str) -> dict[str, Any] | None:
        rows = await self._send("GetNodePropertiesBatch", {"node_ids": [node_id]})
        for row in rows or ():
            if isinstance(row, (list, tuple)) and len(row) == 2 and row[0] == node_id:
                return _props(row[1]) if row[1] is not None else None
        return None

    async def read(self, tenant: str, run_id: str) -> dict[str, Any] | None:
        found = await self._node(run_id)
        # A foreign run answers exactly like an absent one.
        if found is None or found.get("tenant") != tenant:
            return None
        return found

    async def create(self, tenant: str, run_id: str, fields: Mapping[str, Any]) -> None:
        body = {**fields, "type": "AdmissionRun", "node_type": "AdmissionRun"}
        await self._send(
            "CreateNodeIfAbsent",
            {"node_id": run_id, "properties_msgpack": _pack(body)},
        )

    async def swap(
        self,
        tenant: str,
        run_id: str,
        expect: Mapping[str, Any],
        updates: Mapping[str, Any],
    ) -> bool:
        answer = await self._send(
            "CompareAndSetNodeFields",
            {
                "node_id": run_id,
                "conditions_msgpack": _pack({**expect, "tenant": tenant}),
                "updates_msgpack": _pack(updates),
            },
        )
        return _took(answer)

    async def claim_active(self, tenant: str, run_id: str) -> str:
        node = active_node_id(tenant)
        await self._send(
            "CreateNodeIfAbsent",
            {
                "node_id": node,
                "properties_msgpack": _pack(
                    {
                        "type": "AdmissionActive",
                        "node_type": "AdmissionActive",
                        "tenant": tenant,
                        "holder": "",
                    }
                ),
            },
        )
        won = await self._send(
            "CompareAndSetNodeFields",
            {
                "node_id": node,
                "conditions_msgpack": _pack({"holder": ""}),
                "updates_msgpack": _pack({"holder": run_id}),
            },
        )
        if _took(won):
            return run_id
        held = await self._node(node)
        return str((held or {}).get("holder", ""))

    async def release_active(self, tenant: str, run_id: str) -> None:
        await self._send(
            "CompareAndSetNodeFields",
            {
                "node_id": active_node_id(tenant),
                "conditions_msgpack": _pack({"holder": run_id}),
                "updates_msgpack": _pack({"holder": ""}),
            },
        )


class EngineAdmissionGovernance:
    """The governed ``governed.source-authority`` change over ``GovernedChange``."""

    def __init__(self, send: Send) -> None:
        self._send = send

    async def propose(
        self, change_id: str, digest: str, target: str, justification: str
    ) -> None:
        await self._send(
            "GovernedChange",
            {
                "op": {
                    "op": "propose",
                    "request": {
                        "change_id": change_id,
                        "digest": f"sha256:{digest}",
                        "justification": justification,
                        "kind": SOURCE_AUTHORITY_KIND,
                        "proposal_ttl_ms": _PROPOSAL_TTL_MS,
                        "target": target,
                        "window_ms": _WINDOW_MS,
                    },
                }
            },
        )

    async def approve(self, change_id: str, digest: str) -> None:
        await self._send(
            "GovernedChange",
            {
                "op": {
                    "op": "approve",
                    "request": {"change_id": change_id, "digest": f"sha256:{digest}"},
                }
            },
        )

    async def status(self, change_id: str) -> str | None:
        try:
            answer = await self._send(
                "GovernedChange", {"op": {"op": "get", "change_id": change_id}}
            )
        except EngineRefusal as refusal:
            if refusal.code in {"NOT_FOUND", "GOVERNED_CHANGE_NOT_FOUND"}:
                return None
            raise
        if isinstance(answer, Mapping):
            value = answer.get("status") or answer.get("state")
            return str(value) if value else None
        return None


class EngineAdmissionSources:
    """Probe through the federation source probe; the rest through pin-lag methods."""

    def __init__(self, send: Send) -> None:
        self._send = send

    async def probe(self, source_id: str) -> bool:
        try:
            answer = await self._send("ProbeForeignSource", {"name": source_id})
        except EngineRefusal:
            return False
        if isinstance(answer, Mapping):
            return all(
                answer.get(flag) is not False for flag in ("ok", "healthy", "reachable")
            )
        return True

    async def attach(self, source_id: str, tables: Sequence[str]) -> None:
        """Admit one registered source per table (``RegisterForeignSource`` made them).

        Each table's source is read from the engine registry; a table with no
        registration refuses with the engine's own code, so nothing half-attaches.
        """
        await self._each("GetForeignSource", source_id, tables)

    async def detach(self, source_id: str, tables: Sequence[str]) -> None:
        """Detach every table's source; the engine's detach is owner-only and idempotent."""
        await self._each("ForeignSourceDetach", source_id, tables)

    async def _each(self, method: str, source_id: str, tables: Sequence[str]) -> None:
        for table in tables:
            await self._send(method, {"name": registered_name(source_id, table)})

    async def table_counts(
        self, source_id: str, tables: Sequence[str]
    ) -> Mapping[str, int]:
        answer = await self._send(
            "ForeignSourceParity",
            {"name": source_id, "tables": list(tables), "sample": 0},
        )
        counts = answer.get("counts") if isinstance(answer, Mapping) else None
        return dict(counts) if isinstance(counts, Mapping) else {}

    async def sample_digest(
        self, source_id: str, tables: Sequence[str], limit: int
    ) -> str:
        answer = await self._send(
            "ForeignSourceParity",
            {"name": source_id, "tables": list(tables), "sample": limit},
        )
        digest = answer.get("sample_digest") if isinstance(answer, Mapping) else None
        return str(digest or "")
