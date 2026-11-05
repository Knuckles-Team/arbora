"""Durable per-principal pin records that keep items loaded across sessions.

ARBORA-FLEET-R059: a pin is a durable record plus a pinned flag on the live
loaded item. ``InMemoryPinStore`` serves fixtures only; ``EgPinStore`` writes
through the service-authorized engine client's control leases, the same
wiring the served invocation already composes for plan storage, so a restart
of arbora or the engine loses no pin. Control leases are time-bound and the
WebUI repository is in memory, so neither can hold a pin without a medulla
per-principal record kind row; until medulla files that row the lease carries
a far-future expiry.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: The engine record kind an :class:`EgPinStore` writes through control leases.
PIN_RECORD_KIND = "arbora.fleet.pin"

#: Control leases are time-bound; a pin needs no TTL until medulla files the
#: per-principal record kind. A far-future expiry is the interim stand-in
#: (2099-12-31T00:00:00Z).
_PIN_EXPIRY_MS = 4_102_416_000_000

#: The engine's one ``ListControlLeases`` page bound.
_PAGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class PinRecord:
    """One principal's durable claim that an item stays loaded."""

    tenant: str
    principal: str
    item_id: str
    binding: tuple[str, ...]
    pinned_at_ms: int


def _valid_record(record: PinRecord) -> bool:
    return (
        isinstance(record.tenant, str)
        and bool(record.tenant)
        and isinstance(record.principal, str)
        and bool(record.principal)
        and isinstance(record.item_id, str)
        and bool(record.item_id)
        and isinstance(record.binding, tuple)
        and bool(record.binding)
        and all(isinstance(part, str) and part for part in record.binding)
        and type(record.pinned_at_ms) is int
    )


@runtime_checkable
class PinStore(Protocol):
    """The durable pin surface: per-principal lists and a tenant-wide pager."""

    async def list(self, tenant: str, principal: str) -> Sequence[PinRecord]:
        """Every active pin of one principal."""
        ...

    async def put(self, record: PinRecord) -> None:
        """Store one pin; a stored pin with the same binding is a no-op."""
        ...

    async def delete(self, tenant: str, principal: str, item_id: str) -> None:
        """Drop one pin; an absent pin is a no-op."""
        ...

    async def list_tenant(
        self, tenant: str, cursor: str | None, limit: int
    ) -> tuple[Sequence[PinRecord], str | None]:
        """One page of every pin in the tenant; never another tenant's."""
        ...


def pinned_now_ms() -> int:
    """The store-agnostic wall-clock stamp a fresh pin carries."""
    return int(time.time() * 1000)


class InMemoryPinStore:
    """The fixture-only pin store; a process restart loses every pin."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], dict[str, PinRecord]] = {}

    async def list(self, tenant: str, principal: str) -> tuple[PinRecord, ...]:
        return tuple(
            sorted(
                self._records.get((tenant, principal), {}).values(),
                key=lambda record: (record.pinned_at_ms, record.item_id),
            )
        )

    async def put(self, record: PinRecord) -> None:
        if not _valid_record(record):
            raise ValueError("pin record fields are incomplete")
        self._records.setdefault((record.tenant, record.principal), {})[
            record.item_id
        ] = record

    async def delete(self, tenant: str, principal: str, item_id: str) -> None:
        self._records.get((tenant, principal), {}).pop(item_id, None)

    async def list_tenant(
        self, tenant: str, cursor: str | None, limit: int
    ) -> tuple[tuple[PinRecord, ...], str | None]:
        rows: list[tuple[PinRecord, str]] = []
        principals = sorted(
            {principal for owner, principal in self._records if owner == tenant}
        )
        for principal in principals:
            for record in await self.list(tenant, principal):
                rows.append((record, f"{principal}\x1f{record.item_id}"))
        start = 0
        if cursor is not None:
            marks = [mark for _record, mark in rows]
            if cursor not in marks:
                raise ValueError("pin cursor expired")
            start = marks.index(cursor) + 1
        page = rows[start : start + limit]
        next_cursor = page[-1][1] if start + len(page) < len(rows) else None
        return tuple(record for record, _mark in page), next_cursor


class EgPinStore:
    """Carry per-principal pins in the engine's control leases.

    The engine client is the service-authorized one the served invocation
    composes for ``EgPlanStore``; the store adds no second database. The
    engine's ``issue`` never overwrites, so a changed binding revokes the
    prior lease and issues a fresh one under a fresh lease id, keyed by the
    same ``tenant:principal`` identity the session key uses.
    """

    def __init__(self, client: Any) -> None:
        self._leases = client.control_leases

    async def _active(
        self, tenant: str, grant_match: dict[str, Any]
    ) -> list[dict[str, Any]]:
        leases: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self._leases.list(
                tenant=tenant,
                kind=PIN_RECORD_KIND,
                status="active",
                grant_match=grant_match,
                cursor=cursor,
                limit=_PAGE_LIMIT,
            )
            rows = page.get("leases")
            if not isinstance(rows, list):
                raise RuntimeError("engine pin list page is malformed")
            leases.extend(row for row in rows if isinstance(row, dict))
            cursor = page.get("next_cursor")
            if cursor is None:
                return leases

    @staticmethod
    def _record(lease: Mapping[str, Any], tenant: str) -> PinRecord:
        grant = lease.get("grant")
        binding = grant.get("binding") if isinstance(grant, Mapping) else None
        if (
            not isinstance(grant, Mapping)
            or not isinstance(grant.get("principal"), str)
            or not isinstance(grant.get("item_id"), str)
            or not isinstance(binding, list)
            or not all(isinstance(part, str) for part in binding)
            or type(grant.get("pinned_at_ms")) is not int
        ):
            raise ValueError("pin lease grant is malformed")
        return PinRecord(
            tenant=tenant,
            principal=grant["principal"],
            item_id=grant["item_id"],
            binding=tuple(binding),
            pinned_at_ms=grant["pinned_at_ms"],
        )

    async def list(self, tenant: str, principal: str) -> tuple[PinRecord, ...]:
        records = [
            self._record(lease, tenant)
            for lease in await self._active(tenant, {"principal": principal})
        ]
        return tuple(
            sorted(records, key=lambda record: (record.pinned_at_ms, record.item_id))
        )

    async def put(self, record: PinRecord) -> None:
        if not _valid_record(record):
            raise ValueError("pin record fields are incomplete")
        tenant = record.tenant
        principal = record.principal
        item_id = record.item_id
        prior = [
            lease
            for lease in await self._active(
                tenant, {"principal": principal, "item_id": item_id}
            )
        ]
        for lease in prior:
            grant = lease.get("grant")
            if (
                isinstance(grant, Mapping)
                and tuple(grant.get("binding") or ()) == record.binding
            ):
                return
        for lease in prior:
            answer = await self._leases.transition(
                tenant=tenant,
                lease_id=str(lease.get("lease_id")),
                expected_revision=int(lease.get("revision", 1)),
                to="revoked",
                idempotency_key=f"pin-revoke:{lease.get('lease_id')}:{secrets.token_hex(8)}",
            )
            if answer.get("outcome") not in {"applied", "not_found"}:
                raise RuntimeError("engine did not revoke the prior pin lease")
        lease_id = f"fleet-pin:{principal}:{item_id}:{secrets.token_hex(8)}"
        answer = await self._leases.issue(
            tenant=tenant,
            lease_id=lease_id,
            kind=PIN_RECORD_KIND,
            grant={
                "principal": principal,
                "item_id": item_id,
                "binding": list(record.binding),
                "pinned_at_ms": record.pinned_at_ms,
            },
            issued_at_ms=pinned_now_ms(),
            expires_at_ms=_PIN_EXPIRY_MS,
            hard_expires_at_ms=_PIN_EXPIRY_MS,
            idempotency_key=f"pin-issue:{lease_id}",
        )
        if answer.get("outcome") != "issued":
            raise RuntimeError("engine did not issue the pin lease")

    async def delete(self, tenant: str, principal: str, item_id: str) -> None:
        prior = [
            lease
            for lease in await self._active(
                tenant, {"principal": principal, "item_id": item_id}
            )
        ]
        for lease in prior:
            answer = await self._leases.transition(
                tenant=tenant,
                lease_id=str(lease.get("lease_id")),
                expected_revision=int(lease.get("revision", 1)),
                to="revoked",
                idempotency_key=f"pin-revoke:{lease.get('lease_id')}:{secrets.token_hex(8)}",
            )
            if answer.get("outcome") not in {"applied", "not_found"}:
                raise RuntimeError("engine did not revoke the pin lease")

    async def list_tenant(
        self, tenant: str, cursor: str | None, limit: int
    ) -> tuple[tuple[PinRecord, ...], str | None]:
        page = await self._leases.list(
            tenant=tenant,
            kind=PIN_RECORD_KIND,
            status="active",
            grant_match=None,
            cursor=cursor,
            limit=min(max(limit, 1), _PAGE_LIMIT),
        )
        rows = page.get("leases")
        if not isinstance(rows, list):
            raise RuntimeError("engine pin list page is malformed")
        records = tuple(
            self._record(lease, tenant) for lease in rows if isinstance(lease, Mapping)
        )
        return records, page.get("next_cursor")
