"""Durable finance receipt store (ARBORA-DATA-MARKET-R001.1).

``FinanceReceiptStore`` is the one idempotency fence for finance alert
deliveries, schedule runs and DCA proposals. It writes and reads
``FinanceReceipt`` nodes in the engine (MEDULLA-FINANCE-PRIMITIVES-R011.4)
through the caller-bound engine client, so a receipt survives a host restart
and stays inside the tenant that wrote it. The tenant is the verified invoke
context's; a receipt for any other tenant refuses ``AUTH_TENANT_MISMATCH``.

Each receipt state is its own node. The node id is the SHA-256 over the
length-prefixed ``tenantKey``, ``receiptKind``, ``idempotencyKey`` and
``receiptState``; the engine's ``CreateNodeIfAbsent`` makes a second write of
the same id a no-op. ``claim`` therefore returns true exactly once per key.
Any engine failure raises :class:`FinanceReceiptUnavailable` (``UNAVAILABLE``)
and the caller must not acknowledge the work it was fencing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

__all__ = [
    "FinanceReceiptStore",
    "FinanceReceiptUnavailable",
    "ReceiptAuthority",
    "ReceiptKind",
    "ReceiptState",
    "receipt_node_id",
]

ReceiptKind = Literal["alert_delivery", "schedule_run", "dca_proposal"]
ReceiptState = Literal["claimed", "completed"]

_KINDS: frozenset[str] = frozenset({"alert_delivery", "schedule_run", "dca_proposal"})
_STATE_QUERY = (
    "MATCH (r:FinanceReceipt {id: $id}) RETURN r.receiptState AS state LIMIT 2"
)


class FinanceReceiptUnavailable(RuntimeError):
    """The receipt store cannot reach the engine; nothing may be acknowledged."""

    code = "UNAVAILABLE"


class ReceiptAuthority(Protocol):
    """The caller-bound engine node methods the store needs."""

    def create_node_if_absent(
        self, node_id: str, *, properties: Mapping[str, Any]
    ) -> bool: ...

    def query_cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> list[Any]: ...


def receipt_node_id(tenant: str, kind: str, key: str, state: str) -> str:
    """The engine node id of one receipt state (MEDULLA-FINANCE-PRIMITIVES-R011.4)."""

    digest = hashlib.sha256()
    for part in (tenant, kind, key, state):
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class FinanceReceiptStore:
    """Engine-backed ``claim`` / ``complete`` / ``state`` receipts per tenant."""

    def __init__(
        self, authority: ReceiptAuthority, *, clock: Callable[[], str] = _now
    ) -> None:
        self._authority = authority
        self._clock = clock

    def claim(self, tenant: str, kind: ReceiptKind, key: str) -> bool:
        """True exactly once per ``(tenant, kind, key)``; false when already claimed."""

        return self._write(tenant, kind, key, "claimed")

    def complete(self, tenant: str, kind: ReceiptKind, key: str) -> None:
        """Durably record that the claimed work finished."""

        self._write(tenant, kind, key, "completed")

    def state(self, tenant: str, kind: ReceiptKind, key: str) -> ReceiptState | None:
        """The furthest recorded state of one key, or ``None`` when never claimed."""

        _check(tenant, kind, key)
        for state in ("completed", "claimed"):
            node_id = receipt_node_id(tenant, kind, key, state)
            try:
                rows = self._authority.query_cypher(_STATE_QUERY, {"id": node_id})
            except Exception as error:
                raise FinanceReceiptUnavailable(
                    "UNAVAILABLE: finance receipt read failed"
                ) from error
            if isinstance(rows, list) and rows:
                return state  # type: ignore[return-value]
        return None

    def _write(self, tenant: str, kind: str, key: str, state: str) -> bool:
        _check(tenant, kind, key)
        properties = {
            "type": "FinanceReceipt",
            "receiptKind": kind,
            "receiptState": state,
            "tenantKey": tenant,
            "idempotencyKey": key,
            "recordedAt": self._clock(),
        }
        try:
            return bool(
                self._authority.create_node_if_absent(
                    receipt_node_id(tenant, kind, key, state), properties=properties
                )
            )
        except Exception as error:
            raise FinanceReceiptUnavailable(
                "UNAVAILABLE: finance receipt write failed"
            ) from error


def _check(tenant: str, kind: str, key: str) -> None:
    from arbora.security.session import resolve_session

    if tenant != resolve_session().tenant:
        raise PermissionError(
            "AUTH_TENANT_MISMATCH: a finance receipt is written in the caller's tenant"
        )
    if kind not in _KINDS:
        raise ValueError(f"unknown finance receipt kind: {kind!r}")
    if not tenant.strip() or not key.strip():
        raise ValueError("a finance receipt needs a tenant and an idempotency key")
