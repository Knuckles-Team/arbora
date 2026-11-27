"""The fleet-owned delegated-identity ports (ARBORA-IDENTITY-R028.3).

The multiplexer reads the exchange issuer, the claims of the verified acting user and the
audit sink from here. The composition root binds them once from the identity configuration;
an unbound exchange has no issuer and no claims, so an actor-required tool refuses
``IDENTITY_DELEGATION_REQUIRED`` and a process credential never stands in.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["ActorExchange", "claims_for_actor"]


def claims_for_actor(actor: Any) -> Mapping[str, Any] | None:
    """The token claims of one verified acting user, or ``None`` for anything else.

    Only an authenticated human with a tenant and a live credential expiry has claims; a
    service or anonymous actor has none, so no exchange can start from it.
    """
    kind = str(getattr(getattr(actor, "actor_type", None), "value", "") or "")
    expiry = getattr(actor, "credential_expires_at", None)
    if (
        not getattr(actor, "authenticated", False)
        or kind != "human"
        or not getattr(actor, "tenant_id", "")
        or type(expiry) is not int
    ):
        return None
    return {
        "sub": str(actor.actor_id),
        "tenant_id": str(actor.tenant_id),
        "principal_kind": "user",
        "roles": list(getattr(actor, "roles", ()) or ()),
        "scope": " ".join(getattr(actor, "roles", ()) or ()),
        "exp": expiry,
    }


class ActorExchange:
    """Bound once by the composition root; read by the multiplexer's one exchange site."""

    __slots__ = ("_audit", "_issuer", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issuer: Any = None
        self._audit: Callable[[Mapping[str, str]], None] = lambda _event: None

    def bind(self, *, issuer: Any, audit: Callable[[Mapping[str, str]], None]) -> None:
        with self._lock:
            self._issuer = issuer
            self._audit = audit

    @property
    def issuer(self) -> Any:
        return self._issuer

    def claims(self, actor: Any) -> Mapping[str, Any] | None:
        return claims_for_actor(actor) if self._issuer is not None else None

    def audit(self, event: Mapping[str, str]) -> None:
        self._audit(event)
