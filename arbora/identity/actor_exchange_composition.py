"""Compose the delegated-identity exchange issuer into the fleet (ARBORA-IDENTITY-R028.3).

The host holds one :class:`~arbora.identity.issuer.LocalIssuer` for the token exchange: the
issuer name, audience and tenant come from the deployment's identity settings and the signing
key from the one secret backend. Nothing is provisioned here. A deployment that supplies no
issuer, audience or tenant leaves the exchange unbound, so an actor-required tool refuses
``IDENTITY_DELEGATION_REQUIRED`` rather than falling back to a process credential.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

__all__ = ["bind_actor_exchange"]

_audit_log = logging.getLogger("arbora.audit.delegation")


class _ExchangeOnlyAuthority:
    """The credential authority of an exchange-only issuer: it issues no credential."""

    async def resolve_credential(self, credential: str) -> Any:
        raise PermissionError("the exchange issuer issues no credential")


def _emit(event: Mapping[str, str]) -> None:
    _audit_log.info("delegation.exchange", extra={"audit": dict(event)})


def bind_actor_exchange(
    fleet: Any,
    *,
    store_factory: Callable[[], Any] | None = None,
    audit: Callable[[Mapping[str, str]], None] = _emit,
) -> bool:
    """Bind the exchange issuer of ``fleet``; ``False`` when the deployment omits it."""
    from arbora.identity.deployment_settings import identity_deployment
    from arbora.identity.issuer import IssuerSettings, LocalIssuer
    from arbora.security.settings import security_settings

    config = security_settings()
    deployed = identity_deployment()
    issuer = deployed.issuer or config.auth_jwt_issuer
    audience = config.auth_jwt_audience
    tenant = deployed.tenant
    if not issuer or not audience or not tenant:
        return False
    if store_factory is None:
        from arbora.security.secrets import create_secrets_client

        def store_factory() -> Any:
            return create_secrets_client().backend

    signer = LocalIssuer(
        store_factory(),
        IssuerSettings(issuer, audience, tenant),
        _ExchangeOnlyAuthority(),  # type: ignore[arg-type]
    )
    fleet.actor.bind(issuer=signer, audit=audit)
    return True
