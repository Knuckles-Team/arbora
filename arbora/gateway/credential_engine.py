"""The engine seams of the credential authority (ARBORA-OPS-R893, PIN-LAG).

Grants and receipts are engine records; arbora holds none. ``send(method, params, tenant)``
is the service-identity engine sender. A served engine that does not declare the grant
or receipt method refuses ``UNAVAILABLE`` naming it, and no local grant stands in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.gateway.credential_authority import CredentialAuthority
from arbora.pin_lag.medulla import require_methods

GRANT_METHOD = "CheckResourceAccess"
RECEIPT_METHOD = "CredentialReceipt"

Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]


class EngineGrantSource:
    """``GrantSource`` over the engine's ``CheckResourceAccess``."""

    def __init__(self, send: Send) -> None:
        self._send = send

    async def decision(
        self,
        tenant: str,
        agent_id: str,
        resource_ref: str,
        protocol: str,
        operation: str,
    ) -> Mapping[str, Any]:
        require_methods(GRANT_METHOD)
        answer = await self._send(
            GRANT_METHOD,
            {
                "agent_id": agent_id,
                "resource_ref": resource_ref,
                "protocol": protocol,
                "operation": operation,
            },
            tenant,
        )
        if not isinstance(answer, Mapping) or "allow" not in answer:
            raise LookupError("the engine answered no access decision")
        return answer


class EngineReceiptSink:
    """``ReceiptSink``: one engine write per decision."""

    def __init__(self, send: Send) -> None:
        self._send = send

    async def record(self, receipt: Mapping[str, Any]) -> None:
        require_methods(RECEIPT_METHOD)
        await self._send(
            RECEIPT_METHOD,
            {"op": {"op": "record", "receipt": dict(receipt)}},
            str(receipt["tenant"]),
        )


def engine_authority(send: Send, clock_ms: Callable[[], int]) -> CredentialAuthority:
    """The host's authority over the served engine's grants and receipts."""
    return CredentialAuthority(
        EngineGrantSource(send), EngineReceiptSink(send), clock_ms
    )


__all__ = ["EngineGrantSource", "EngineReceiptSink", "engine_authority"]
