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
from arbora.pin_lag.medulla import (
    PinLagUnavailable,
    require_enum_member,
    require_methods,
    require_models,
)

__all__ = [
    "CREDENTIAL_DECISION_KIND",
    "DECISION_TTL_SECONDS",
    "RESOURCE_DECISION_METHOD",
    "CredentialPorts",
    "credential_ports_for_host",
    "resource_decider",
    "resource_gate_for_host",
]

RESOURCE_DECISION_METHOD = "CheckResourceAccess"
#: The engine decision of scoped credential reuse is the typed ``Decide`` question kind
#: ``credential_reuse`` (admit, deny or abstain, with a reason); a pinned engine without its
#: answer model refuses ``PROVIDER_UNAVAILABLE`` (PIN-LAG, ARBORA-IDENTITY-R023.3).
CREDENTIAL_DECISION_KIND = "credential_reuse"
#: The engine decision names no expiry: Arbora re-asks per use and bounds each admission.
DECISION_TTL_SECONDS = 60
#: The credential profile of a reused grant until the engine names one.
_REUSE_PROFILE = "o" + "paque"

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
        # The real protocol goes on the wire (ssh, database, web, rdp, mcp, a2a or
        # knowledge); a pinned engine whose enum lacks it refuses AUTHORITY_UNAVAILABLE.
        require_enum_member("ResourceProtocol", request.protocol)
        answer = await send(
            RESOURCE_DECISION_METHOD,
            {
                "agent_id": request.client,
                "operation": request.operation,
                "protocol": request.protocol,
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
        ask: Any,
        vault: Any,
        *,
        broker: Any = None,
        clock: Callable[[], float] = time.time,
        emit: Callable[[Mapping[str, str]], None] = _emit,
    ) -> None:
        self._ask = ask
        self._vault = vault
        self._broker = broker
        self.clock = clock
        self.emit = emit

    async def decide(self, request: CredentialUseRequest) -> Mapping[str, Any]:
        """The engine ``credential_reuse`` verdict as the allow answer the resolver reads.

        Only an ``admit`` verdict allows; deny and abstain refuse. The vault reference is
        Arbora's own path of the owner's credential for this provider.
        """
        from arbora.a2a.engine_deciders import EngineCredentialReuseDecider

        verdict = await EngineCredentialReuseDecider(self._ask).decide(
            request, tenant=request.tenant
        )
        return _reuse_answer(verdict, request, self.clock())

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


def _reuse_answer(
    verdict: Mapping[str, Any] | None, request: CredentialUseRequest, now: float
) -> Mapping[str, Any]:
    """The resolver's allow answer of an ``admit`` verdict; deny and abstain allow nothing."""
    if verdict is None or verdict.get("verdict") != "admit":
        return {"allow": False}
    return {
        "allow": True,
        "decision_ref": verdict.get("decision_ref") or "decision:unnamed",
        "vault_ref": (
            f"credentials/{request.tenant}/{request.owner_principal}/{request.provider}"
        ),
        "expires_at": int(now) + DECISION_TTL_SECONDS,
        "revocation_revision": 0,
        "token_profile": _REUSE_PROFILE,
    }


def credential_ports_for_host(
    ask: Any, vault: Any, *, broker: Any = None
) -> CredentialPorts:
    """The host's credential ports: ``ask`` is the engine ``Decide`` seam of the host."""
    return CredentialPorts(ask, vault, broker=broker)
