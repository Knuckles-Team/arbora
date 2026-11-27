"""The credential access authority arbora serves to the SDK (ARBORA-OPS-R893).

A connector resolves a secret only under a ``CredentialAccessBinding`` the host granted
(``SCION-CONNECTOR-CONTROL-R035.1``). :class:`CredentialAuthority` builds that binding
only from the exact current grant record, writes one receipt per decision, and answers the
SDK's synchronous ``CredentialAuthorityPort.verify`` for the binding it admitted and for
no other. The grant lives in the engine; arbora holds none. The SDK types come from
``arbora.pin_lag.scion_sdk`` so a pinned release without them refuses ``UNAVAILABLE``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from contextvars import ContextVar
from typing import Any, Protocol

from arbora.pin_lag.scion_sdk import require_credential_binding
from arbora.pin_lag.unavailable import PinLagUnavailable

#: The four closed refusal reasons of the authority.
REFUSALS: tuple[str, ...] = (
    "scope_denied",
    "binding_mismatch",
    "grant_expired",
    "authority_unavailable",
)

#: The engine's closed ``ResourceAccessDecision.reason_code`` set, mapped exactly
#:. A revoked grant leaves the ledger and reads as missing, so a
#: revoked, a foreign and an absent grant answer alike.
DENIAL_REASONS: dict[str, str] = {
    "RESOURCE_UNKNOWN": "binding_mismatch",
    "RESOURCE_GRANT_MISSING": "binding_mismatch",
    "GRANT_EXPIRED": "grant_expired",
}


class CredentialDenied(PermissionError):
    """One closed refusal; ``reason`` names it and no value is carried."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"credential access denied: {reason}")
        self.reason = reason


class GrantSource(Protocol):
    """The engine's resource grant ledger, read with ``CheckResourceAccess``."""

    async def decision(
        self,
        tenant: str,
        agent_id: str,
        resource_ref: str,
        protocol: str,
        operation: str,
    ) -> Mapping[str, Any]:
        """A ``ResourceAccessDecision {allow, grant_id, scope_ceiling, expires_at_ms,
        revocation_revision, reason_code}``."""
        ...


class ReceiptSink(Protocol):
    """Where one receipt per decision is written (an engine write)."""

    async def record(self, receipt: Mapping[str, Any]) -> None: ...


Clock = Callable[[], int]
_ADMITTED: ContextVar[frozenset[str]] = ContextVar("arbora_credential_admitted")


class CredentialAuthority:
    """Admits one credentialed child call against the exact current grant."""

    def __init__(self, grants: GrantSource, receipts: ReceiptSink, clock_ms: Clock):
        self._grants = grants
        self._receipts = receipts
        self._clock_ms = clock_ms

    def verify(self, binding: Any) -> str:
        """The SDK port: ``ok`` only for a binding this call's :meth:`admitted` admitted."""
        module = require_credential_binding()
        admitted = _ADMITTED.get(frozenset())
        return (
            "ok" if module.binding_digest(binding) in admitted else "binding_mismatch"
        )

    @contextlib.asynccontextmanager
    async def admitted(
        self,
        *,
        tenant: str,
        requesting_principal: str,
        client: str,
        connector: str,
        operation: str,
        resource: str,
        protocol: str,
        profile: Mapping[str, Any],
        scopes: tuple[str, ...] = (),
    ) -> AsyncIterator[Any]:
        """The binding for one call, with the SDK's binding bound for its duration.

        ``profile`` is the trusted connector admission's own facts (provider, audience,
        vault reference, token profile and revision); the grant ledger supplies the
        grant, its scope ceiling, expiry and revocation revision.
        """
        module = require_credential_binding()
        ask = {
            "tenant": tenant,
            "requesting_principal": requesting_principal,
            "client": client,
            "connector": connector,
            "operation": operation,
            "resource": resource,
            "protocol": protocol,
            "profile": profile,
            "scopes": scopes,
        }
        binding = await self._admit(module, ask)
        token = _ADMITTED.set(frozenset({module.binding_digest(binding)}))
        try:
            with module.use_credential_binding(binding):
                yield binding
        finally:
            _ADMITTED.reset(token)

    async def _admit(self, module: Any, ask: Mapping[str, Any]) -> Any:
        try:
            reason, binding = await self._decide(module, ask)
        except PinLagUnavailable:
            raise
        except Exception:
            reason, binding = "authority_unavailable", None
        await self._receipt(ask, reason, binding, module)
        if reason != "ok":
            raise CredentialDenied(reason)
        return binding

    async def _decide(self, module: Any, ask: Mapping[str, Any]) -> tuple[str, Any]:
        decision = await self._grants.decision(
            ask["tenant"],
            ask["client"],
            ask["resource"],
            ask["protocol"],
            ask["operation"],
        )
        reason = self._verdict(decision, ask)
        if reason != "ok":
            return reason, None
        return "ok", module.CredentialAccessBinding(**self._fields(decision, ask))

    def _verdict(self, decision: Mapping[str, Any], ask: Mapping[str, Any]) -> str:
        if not decision.get("allow"):
            # A code outside the closed set is a contract break: it raises and the
            # decision refuses ``authority_unavailable``.
            return DENIAL_REASONS[str(decision.get("reason_code"))]
        if int(decision.get("expires_at_ms") or 0) <= self._clock_ms():
            return "grant_expired"
        if not set(ask["scopes"]) <= set(decision.get("scope_ceiling") or ()):
            return "scope_denied"
        return "ok"

    @staticmethod
    def _fields(decision: Mapping[str, Any], ask: Mapping[str, Any]) -> dict[str, Any]:
        profile = ask["profile"]
        granted = tuple(decision.get("scope_ceiling") or ())
        return {
            "tenant": ask["tenant"],
            "owner_principal": str(
                profile.get("owner_principal") or ask["requesting_principal"]
            ),
            "requesting_principal": ask["requesting_principal"],
            "client": ask["client"],
            "connector": ask["connector"],
            "provider": profile["provider"],
            "audience": profile["audience"],
            "scopes": tuple(sorted(ask["scopes"] or granted)),
            "resource": ask["resource"],
            "operation": ask["operation"],
            "grant_ref": str(decision["grant_id"]),
            "vault_ref": profile["vault_ref"],
            "expires_at_ms": int(decision["expires_at_ms"]),
            "revocation_revision": str(decision["revocation_revision"]),
            "token_profile": profile["token_profile"],
            "profile_revision": str(profile["profile_revision"]),
        }

    async def _receipt(
        self, ask: Mapping[str, Any], reason: str, binding: Any, module: Any
    ) -> None:
        await self._receipts.record(
            {
                "tenant": ask["tenant"],
                "client": ask["client"],
                "connector": ask["connector"],
                "resource": ask["resource"],
                "operation": ask["operation"],
                "outcome": "ok" if reason == "ok" else reason,
                "binding_digest": _receipt_digest(ask, binding, module),
                "at_ms": self._clock_ms(),
            }
        )


def _receipt_digest(ask: Mapping[str, Any], binding: Any, module: Any) -> str:
    """The receipt's ``sha256:<64 hex>`` digest the engine requires (MEDULLA-IDENTITY-R753).

    An admitted call carries its binding's digest; a refused one has no binding, so the
    digest is the ask's own: the same refusal of the same ask records the same digest.
    """
    import hashlib
    import json

    if binding is not None:
        return "sha256:" + str(module.binding_digest(binding)).removeprefix("sha256:")
    keys = ("tenant", "client", "connector", "resource", "operation")
    canonical = json.dumps(
        {key: ask[key] for key in keys}, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


__all__ = ["DENIAL_REASONS", "REFUSALS", "CredentialAuthority", "CredentialDenied"]
