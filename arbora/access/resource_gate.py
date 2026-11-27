"""The resource gate at ``fleet.call`` (ARBORA-IDENTITY-R019.3 to R019.5).

A connector tool that declares ``resource_protocol`` and ``resource_param`` is a resource
tool: before any upstream connection the gate asks the engine for the decision
(``CheckResourceAccess``), re-checks it with :func:`evaluate`, and refuses by default. A
tool without the declaration skips the gate; the shared invoke policy path stays mandatory.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from types import MappingProxyType, SimpleNamespace
from typing import Any

from arbora.access.resource_grants import (
    ResourceAccessRequest,
    ResourceGrant,
    ResourceRefusal,
    evaluate,
)
from arbora.security.audit_event import audit_event
from arbora.security.operation_result import OpError

__all__ = ["ENFORCERS", "PUBLIC_DENIAL", "ResourceGate", "public_code"]

#: The shared public code of a denied or unknown resource.
PUBLIC_DENIAL = "RESOURCE_ACCESS_DENIED"
#: Protocol enforcement is the connector call itself. No ``rdp`` connector declares it.
ENFORCERS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "ssh": frozenset({"tunnel-manager"}),
        "database": frozenset({"sql-mcp"}),
        "web": frozenset(),  # any admitted web connector that declares the protocol
        "rdp": frozenset(),
    }
)
_PASS_THROUGH = frozenset(
    {
        ResourceRefusal.AUTHORITY_UNAVAILABLE,
        ResourceRefusal.UNSUPPORTED_ENFORCEMENT,
    }
)

#: Asks the engine ``CheckResourceAccess`` and answers its ``ResourceAccessDecision``.
Decide = Callable[[ResourceAccessRequest], Awaitable[Mapping[str, Any]]]


def public_code(refusal: ResourceRefusal) -> str:
    """Denied, unknown, stale, expired and tenant-foreign grants answer one code."""
    return refusal.value if refusal in _PASS_THROUGH else PUBLIC_DENIAL


class ResourceGate:
    def __init__(
        self,
        *,
        decide: Decide,
        clock: Callable[[], float],
        emit: Callable[[Mapping[str, str]], None],
        trusted_roles: Callable[[Any], Iterable[str]] = lambda _caller: (),
        enforcers: Mapping[str, frozenset[str]] = ENFORCERS,
    ) -> None:
        self._decide = decide
        self._clock = clock
        self._emit = emit
        self._roles = trusted_roles
        self._enforcers = enforcers

    async def __call__(
        self, op: Any, params: Mapping[str, Any], caller: Any, decision: Any
    ) -> OpError | None:
        protocol = getattr(decision, "resource_protocol", None)
        param = getattr(decision, "resource_param", None)
        if not protocol or not param:
            return None  # not a resource tool
        return await self.require_resource_grant(
            op, params, caller, protocol=str(protocol), param=str(param)
        )

    async def require_resource_grant(
        self,
        op: Any,
        params: Mapping[str, Any],
        caller: Any,
        *,
        protocol: str,
        param: str,
    ) -> OpError | None:
        arguments = params.get("arguments") or {}
        server = str(params.get("server") or "")
        request: ResourceAccessRequest | None = None
        grant: ResourceGrant | None = None
        refusal: ResourceRefusal | None = None
        try:
            request = ResourceAccessRequest(
                tenant=str(caller.tenant),
                principal=str(caller.principal),
                client=str(getattr(caller, "client", None) or caller.principal),
                resource_ref=str(arguments.get(param, "")),
                protocol=protocol,
                operation=str(params.get("tool") or ""),
            )
        except ValueError:
            refusal = ResourceRefusal.UNKNOWN
        if request is not None and not self._enforced(protocol, server):
            refusal = ResourceRefusal.UNSUPPORTED_ENFORCEMENT
        if request is not None and refusal is None:
            grant, refusal = await self._grant(request, caller)
        if refusal is None and request is not None and grant is not None:
            verdict = evaluate(
                request,
                grant,
                int(self._clock() * 1000),
                grant.revocation_revision,
                resource_version=grant.resource_version,
                trusted_roles=self._roles(caller),
            )
            refusal = verdict.refusal
        self._audit(op, params, caller, request, grant, refusal)
        return None if refusal is None else OpError(public_code(refusal))

    def _enforced(self, protocol: str, server: str) -> bool:
        if protocol == "rdp" or protocol not in self._enforcers:
            return False
        servers = self._enforcers[protocol]
        return not servers or server in servers

    async def _grant(
        self, request: ResourceAccessRequest, caller: Any
    ) -> tuple[ResourceGrant | None, ResourceRefusal | None]:
        try:
            answer = await self._decide(request)
        except Exception:
            logging.getLogger(__name__).debug(
                "an unanswered question grants nothing", exc_info=True
            )
            return None, ResourceRefusal.AUTHORITY_UNAVAILABLE
        if not answer.get("allow"):
            return None, ResourceRefusal.GRANT_MISSING
        # A role subject is judged by ``evaluate`` against the caller's trusted roles.
        subject = str(answer.get("subject") or f"user:{request.principal}")
        try:
            return (
                ResourceGrant(
                    engine_decision_ref=str(answer.get("grant_id") or ""),
                    subject=subject,
                    tenant=request.tenant,
                    resource_ref=request.resource_ref,
                    resource_version=str(answer.get("resource_version") or ""),
                    protocol=request.protocol,
                    operation=request.operation,
                    scope_ceiling=tuple(answer.get("scope_ceiling") or ()),
                    expires_at=int(answer.get("expires_at_ms") or 0),
                    revocation_revision=int(answer.get("revocation_revision", -1)),
                ),
                None,
            )
        except (TypeError, ValueError):
            return None, ResourceRefusal.GRANT_MISSING

    def record_enforcement(
        self,
        op: Any,
        params: Mapping[str, Any],
        caller: Any,
        grant: ResourceGrant,
        outcome: str,
    ) -> None:
        """Evidence from the connector boundary: grant expiry, revocation, termination."""
        self._audit(op, params, caller, None, grant, None, enforcement=outcome)

    def _audit(
        self,
        op: Any,
        params: Mapping[str, Any],
        caller: Any,
        request: ResourceAccessRequest | None,
        grant: ResourceGrant | None,
        refusal: ResourceRefusal | None,
        enforcement: str | None = None,
    ) -> None:
        evidence = {
            "resource_ref": request.resource_ref if request else "",
            "protocol": request.protocol
            if request
            else (grant.protocol if grant else ""),
            "resource_operation": request.operation
            if request
            else (grant.operation if grant else ""),
            "resource_version": grant.resource_version if grant else "",
            "engine_decision_ref": grant.engine_decision_ref if grant else "",
            "client": request.client if request else "",
            "enforcement": enforcement or ("refused" if refusal else "permitted"),
        }
        if grant is not None:
            evidence["grant_expires_at"] = str(grant.expires_at)
        if refusal is not None:
            evidence["refusal_code"] = refusal.value
        surface = getattr(caller, "surface", None) or SimpleNamespace(value="fleet")
        event = audit_event(
            op,
            params,
            caller,
            surface,
            "refused" if refusal else "ok",
            evidence={k: v for k, v in evidence.items() if v != ""},
        )
        self._emit(event)
