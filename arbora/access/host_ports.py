"""The host composition of the resource gate and credential reuse (PIN-LAG).

``ARBORA-IDENTITY-R019.3`` and ``ARBORA-IDENTITY-R023.3`` name ports that this module
composes into the serving host, behind the medulla capability guard: a pinned release
without the engine decision method refuses ``AUTHORITY_UNAVAILABLE`` (the resource gate)
or ``PROVIDER_UNAVAILABLE`` (credential reuse) and nothing falls back to a local grant.
Delete the guards with the re-pin that carries the engine methods.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.access.credential_use import (
    AuthorizationChallenge,
    CredentialUseGrant,
    CredentialUseRequest,
)
from arbora.access.resource_gate import ResourceGate
from arbora.access.resource_grants import ResourceAccessRequest
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods, require_models

__all__ = [
    "CREDENTIAL_DECISION_METHOD",
    "RESOURCE_DECISION_METHOD",
    "CredentialPorts",
    "credential_ports_for_host",
    "resource_decider",
    "resource_gate_for_host",
]

RESOURCE_DECISION_METHOD = "CheckResourceAccess"
#: The engine decision of scoped credential reuse; engine ``main`` does not serve it yet,
#: so every use refuses ``PROVIDER_UNAVAILABLE`` (listed in the cross-repo gaps).
CREDENTIAL_DECISION_METHOD = "CheckCredentialUse"
#: The engine's ``ResourceProtocol`` is the transport of the call: a connector tool call
#: is an MCP call, whatever protocol the connector enforces on the resource.
_WIRE_PROTOCOL = "mcp"

#: ``send(method, params, tenant)``: the engine as the host service identity.
EngineSend = Callable[[str, dict[str, Any], str], Awaitable[Any]]
_audit_log = logging.getLogger("arbora.audit.access")


def resource_decider(
    send: EngineSend,
) -> Callable[[ResourceAccessRequest], Awaitable[Mapping[str, Any]]]:
    """The gate's decision client: one engine ``CheckResourceAccess`` per request."""

    async def decide(request: ResourceAccessRequest) -> Mapping[str, Any]:
        require_methods(RESOURCE_DECISION_METHOD)
        require_models(("ResourceAccessDecision",))
        answer = await send(
            RESOURCE_DECISION_METHOD,
            {
                "agent_id": request.client,
                "operation": request.operation,
                "protocol": _WIRE_PROTOCOL,
                "resource_ref": request.resource_ref,
            },
            request.tenant,
        )
        return answer if isinstance(answer, Mapping) else {}

    return decide


def _emit(event: Mapping[str, str]) -> None:
    _audit_log.info("access.evidence", extra={"audit": dict(event)})


def resource_gate_for_host(
    send: EngineSend,
    *,
    clock: Callable[[], float] = time.time,
    emit: Callable[[Mapping[str, str]], None] = _emit,
) -> ResourceGate:
    """The one resource gate the invoke pipeline runs for resource tools."""
    return ResourceGate(decide=resource_decider(send), clock=clock, emit=emit)


class CredentialPorts:
    """The host's :class:`~arbora.access.credential_use.CredentialUse` ports.

    The engine decision, the host vault and token standing are composed here. The PKCE
    ceremony (``challenge``, ``complete``) and ``refresh`` belong to the remote OAuth
    broker; until the broker is bound a missing credential answers ``INTERACTION_REQUIRED``
    and an expired one refuses, never a shared credential.
    """

    def __init__(
        self,
        send: EngineSend,
        vault: Any,
        *,
        broker: Any = None,
        clock: Callable[[], float] = time.time,
        emit: Callable[[Mapping[str, str]], None] = _emit,
    ) -> None:
        self._send = send
        self._vault = vault
        self._broker = broker
        self.clock = clock
        self.emit = emit

    async def decide(self, request: CredentialUseRequest) -> Mapping[str, Any]:
        require_methods(CREDENTIAL_DECISION_METHOD)
        answer = await self._send(
            CREDENTIAL_DECISION_METHOD,
            {
                "owner": request.owner_principal,
                "agent_id": request.client,
                "connector": request.connector,
                "provider": request.provider,
                "scopes": list(request.scopes),
                "purpose": request.operation,
            },
            request.tenant,
        )
        return answer if isinstance(answer, Mapping) else {}

    async def vault_get(self, ref: str) -> Mapping[str, Any] | None:
        import asyncio
        import json

        raw = await asyncio.to_thread(self._vault.get, ref)
        return json.loads(raw) if raw else None

    async def vault_put(self, ref: str, record: Mapping[str, Any]) -> str:
        import asyncio
        import json

        await asyncio.to_thread(self._vault.set, ref, json.dumps(dict(record)))
        return f"vault:{ref}"

    async def validate(
        self, record: Mapping[str, Any], grant: CredentialUseGrant
    ) -> str:
        expiry = record.get("expires_at")
        if record.get("revoked"):
            return "revoked"
        if isinstance(expiry, (int, float)) and expiry <= self.clock():
            return "expired"
        return "valid"

    async def refresh(
        self, record: Mapping[str, Any], grant: CredentialUseGrant
    ) -> Mapping[str, Any]:
        if self._broker is None:
            raise PinLagUnavailable("RemoteOAuthBroker.refresh")
        return await self._broker.refresh(record, grant)

    async def challenge(
        self, request: CredentialUseRequest, attempt: int
    ) -> AuthorizationChallenge | None:
        if self._broker is None:
            return None
        return await self._broker.challenge(request, attempt)

    async def complete(
        self, challenge: AuthorizationChallenge, deadline: float
    ) -> Mapping[str, Any]:
        if self._broker is None:
            raise PinLagUnavailable("RemoteOAuthBroker.complete")
        return await self._broker.complete(challenge, deadline)


def credential_ports_for_host(
    send: EngineSend, vault: Any, *, broker: Any = None
) -> CredentialPorts:
    return CredentialPorts(send, vault, broker=broker)
