"""Finance receipts through the engine machine (ARBORA-DATA-MARKET-R001.1).

Arbora holds no receipt store. The engine owns the ``FinanceReceipt`` node and
its delivery machine (MEDULLA-FINANCE-PRIMITIVES-R011.4 and R011.5). A receipt
id is the SHA-256 of the length-prefixed ``tenantKey``, ``receiptKind`` and
``idempotencyKey``. A receipt starts ``due`` at attempt 1. The engine moves it
with ``FinanceReceiptTransition``, so ``claim`` succeeds once per key because a
second ``Claim`` from ``claimed`` is refused ``TRANSITION_REFUSED``. These
functions send those two methods through the engine port and keep no state.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

import msgpack

from arbora.api.errors import EngineRefusal

__all__ = [
    "FinanceReceiptEngine",
    "ReceiptKind",
    "ServedReceiptEngine",
    "claim_receipt",
    "complete_receipt",
    "receipt_id",
]

ReceiptKind = Literal["alert_delivery", "schedule_run", "dca_proposal"]
_KINDS: frozenset[str] = frozenset({"alert_delivery", "schedule_run", "dca_proposal"})

#: ``(method, params)`` to the caller-bound engine, as ``engine_send`` does.
Send = Callable[[str, dict[str, Any]], Awaitable[Any]]


class FinanceReceiptEngine(Protocol):
    """The two engine sends a receipt needs; each refusal keeps the engine code."""

    async def open(self, receipt: str, fields: Mapping[str, Any]) -> None: ...

    async def move(self, receipt: str, event: str) -> bool:
        """True when the engine took the event; false on ``TRANSITION_REFUSED``."""
        ...


def receipt_id(tenant: str, kind: str, key: str) -> str:
    """The engine node id of one receipt (MEDULLA-FINANCE-PRIMITIVES-R011.4)."""
    digest = hashlib.sha256()
    for part in (tenant, kind, key):
        encoded = part.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


class ServedReceiptEngine:
    """The engine port over one verified caller's ``engine_send``."""

    def __init__(self, send: Send) -> None:
        self._send = send

    async def open(self, receipt: str, fields: Mapping[str, Any]) -> None:
        await self._send(
            "CreateNodeIfAbsent",
            {
                "node_id": receipt,
                "properties_msgpack": msgpack.packb(dict(fields), use_bin_type=True),
            },
        )

    async def move(self, receipt: str, event: str) -> bool:
        try:
            await self._send(
                "FinanceReceiptTransition", {"receipt_id": receipt, "event": event}
            )
        except EngineRefusal as refusal:
            if refusal.code == "TRANSITION_REFUSED":
                return False
            raise
        return True


def _check(tenant: str, kind: str, key: str) -> None:
    if kind not in _KINDS:
        raise ValueError(f"unknown finance receipt kind: {kind!r}")
    if not tenant.strip() or not key.strip():
        raise ValueError("a finance receipt needs a tenant and an idempotency key")


async def claim_receipt(
    engine: FinanceReceiptEngine,
    tenant: str,
    kind: ReceiptKind,
    key: str,
    *,
    event_id: str,
    mode: str = "paper",
    channel: str = "none",
    recorded_at: str,
) -> bool:
    """True exactly once per ``(tenant, kind, key)``; false when already claimed.

    An engine failure propagates, and the caller must not acknowledge the work.
    """
    _check(tenant, kind, key)
    receipt = receipt_id(tenant, kind, key)
    await engine.open(
        receipt,
        {
            "type": "FinanceReceipt",
            "receiptKind": kind,
            "receiptState": "due",
            "tenantKey": tenant,
            "idempotencyKey": key,
            "recordedAt": recorded_at,
            "receiptEventId": event_id,
            "receiptMode": mode,
            "receiptChannel": channel,
            "receiptAttempt": 1,
        },
    )
    return await engine.move(receipt, "claim")


async def complete_receipt(
    engine: FinanceReceiptEngine, tenant: str, kind: ReceiptKind, key: str
) -> None:
    """Move a claimed receipt through ``deliver`` and ``complete``."""
    _check(tenant, kind, key)
    receipt = receipt_id(tenant, kind, key)
    for event in ("deliver", "complete"):
        if not await engine.move(receipt, event):
            raise RuntimeError(f"finance receipt refused {event}")
