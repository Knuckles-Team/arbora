"""The per-provider remote-OAuth brokers of one fleet (ARBORA-HOST-R039.2).

A remote MCP child whose catalog entry declares an ``oauth_provider`` block is
called with a bearer bound to the CURRENT caller's own delegated grant. One
:class:`~arbora.fleet.remote_oauth_broker.RemoteOAuthBroker` is kept per
``provider_id`` so its encrypted token store and dynamic-client registration
cache persist across calls. :class:`RemoteOAuthBrokers` owns that cache and its
lock; the host builds one per fleet and the multiplexer reaches it by argument.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

__all__ = ["RemoteOAuthBrokers", "oauth_gated"]


def oauth_gated(cfg: Mapping[str, Any]) -> bool:
    """True when a remote MCP child catalog entry declares a per-principal
    OAuth provider (``oauth_provider``, a ``ProviderDescriptor``-shaped dict).
    ``False`` (including for any malformed value) leaves the existing
    service-credential/static-header path completely unchanged -- validation
    of a truthy value happens in :meth:`RemoteOAuthBrokers.broker_for`, which
    fails closed on a bad admin config rather than silently ignoring it."""
    provider_cfg = cfg.get("oauth_provider")
    return isinstance(provider_cfg, dict) and bool(provider_cfg)


class RemoteOAuthBrokers:
    """One broker per admin-declared OAuth provider, created on first use."""

    __slots__ = ("_brokers", "_lock")

    def __init__(self) -> None:
        self._brokers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def broker_for(self, provider_cfg: Mapping[str, Any]) -> tuple[Any, Any]:
        """Return ``(RemoteOAuthBroker, ProviderDescriptor)`` for one admin-declared
        provider block, reusing ONE broker instance per ``provider_id`` so its
        encrypted token store and DCR registration cache persist across calls
        instead of being rebuilt (and, for DCR, re-registered) on every request."""
        from arbora.fleet.remote_oauth_broker import (
            ProviderDescriptor,
            ProviderRegistry,
            RemoteOAuthBroker,
        )

        descriptor = ProviderDescriptor(**provider_cfg)
        with self._lock:
            broker = self._brokers.get(descriptor.provider_id)
            if broker is None:
                registry = ProviderRegistry()
                registry.register(descriptor)
                broker = RemoteOAuthBroker(registry=registry)
                self._brokers[descriptor.provider_id] = broker
            else:
                # Keep the registered descriptor current across a hot catalog
                # reload (e.g. a scope/enabled-flag change) without discarding the
                # broker's token store / DCR cache.
                broker.registry.register(descriptor)
        return broker, descriptor

    def bearer(self, cfg: Mapping[str, Any], url: str) -> dict[str, str] | None:
        """Per-principal bearer resolution for an OAuth-gated remote MCP child.

        Returns ``None`` when the child is not OAuth-gated (the existing
        service-credential/static-header path proceeds completely unchanged).
        When gated, resolves the VERIFIED current actor
        (:func:`arbora.security.actor_context.current_actor` -- the same
        server-minted identity every other authorization decision in this gateway
        uses, never a caller-supplied string) and mints a bearer bound to the
        EXACT registered resource endpoint via
        :meth:`~arbora.fleet.remote_oauth_broker.RemoteOAuthBroker.bearer_headers_for`.
        A missing, expired, or revoked grant raises -- fail closed, no fallback to
        any shared/service credential (U-44/U-45).

        Synchronous (secrets-backend I/O, no coroutine) -- callers run this via
        ``asyncio.to_thread`` so it never blocks the event loop.
        """
        if not oauth_gated(cfg):
            return None
        from arbora.security.actor_context import current_actor

        actor = (
            current_actor()
        )  # IdentityRequiredError (PermissionError) -> fail closed
        broker, descriptor = self.broker_for(cfg["oauth_provider"])
        return broker.bearer_headers_for(
            actor=actor, provider_id=descriptor.provider_id, resource_url=url
        )

    def grant(
        self, cfg: Mapping[str, Any], url: str
    ) -> tuple[dict[str, str], Any] | None:
        """Resolve one bearer plus the broker-owned, non-secret grant binding."""

        if not oauth_gated(cfg):
            return None
        from arbora.security.actor_context import current_actor

        actor = current_actor()
        broker, descriptor = self.broker_for(cfg["oauth_provider"])
        return broker.bearer_headers_and_grant_binding(
            actor=actor, provider_id=descriptor.provider_id, resource_url=url
        )

    def grant_bindings(self, actor: Any) -> tuple[Any, ...]:
        """Return current broker-resolved grants for a verified actor.

        The registry uses this process-owned broker inventory for its SQL predicate;
        it never accepts provider/resource/audience/grant identity from a request.
        Missing, expired, revoked, or legacy token records are omitted so reads fail
        closed when no exact grant remains.
        """

        from arbora.fleet.discovery_authority import (
            OAuthGrantBinding,
        )
        from arbora.fleet.remote_oauth_broker import (
            OAuthProviderError,
            OAuthScopeError,
            OAuthTokenAbsentError,
            OAuthTokenStore,
        )

        OAuthTokenStore._require_verified(actor)
        with self._lock:
            brokers = tuple(self._brokers.values())
        bindings: list[OAuthGrantBinding] = []
        for broker in brokers:
            for provider in broker.registry.enabled_providers():
                try:
                    binding = broker.grant_binding_for(
                        actor=actor,
                        provider_id=provider.provider_id,
                        resource_url=provider.resource_url,
                    )
                except (
                    OAuthTokenAbsentError,
                    OAuthProviderError,
                    OAuthScopeError,
                    PermissionError,
                ):
                    continue
                if isinstance(binding, OAuthGrantBinding):
                    bindings.append(binding)
        return tuple(sorted(bindings, key=lambda binding: binding.fingerprint))
