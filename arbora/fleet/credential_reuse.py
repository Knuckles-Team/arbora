"""The fleet-owned scoped credential reuse of remote OAuth connectors (ARBORA-IDENTITY-R023.2).

One holder per fleet. The composition root binds the ports once (:func:`bind_credential_reuse`);
the multiplexer's remote-child connect is the production caller of :meth:`CredentialReuse.bearer`
for a connector entry that declares a ``credential_owner``. An unbound holder refuses
``PROVIDER_UNAVAILABLE`` and a process or shared credential never stands in.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from arbora.access.credential_use import (
    CredentialRefusal,
    CredentialRefused,
    CredentialUseRequest,
    resolve_credential,
)

__all__ = ["CredentialReuse", "bind_credential_reuse"]

#: The ``fleet.call`` operation a connect for a reused credential stands for.
CONNECT_OPERATION = "connect"


class CredentialReuse:
    """Bound once by the composition root; read by the remote-child connect."""

    __slots__ = ("_lock", "_ports")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ports: Any = None

    def bind(self, ports: Any) -> None:
        with self._lock:
            self._ports = ports

    @property
    def bound(self) -> bool:
        return self._ports is not None

    async def bearer(
        self, caller: Any, cfg: Mapping[str, Any], url: str, provider: Any
    ) -> dict[str, str]:
        """The ``Authorization`` header of the reused credential, or raise a refusal.

        The connector is the admitted catalog entry, the owner is the operator-declared
        ``credential_owner``; no request field names either.
        """
        ports = self._ports
        if ports is None:
            raise CredentialRefused(CredentialRefusal.PROVIDER_UNAVAILABLE)
        request = CredentialUseRequest.from_verified(
            caller,
            {
                "provider": provider.provider_id,
                "audience": url,
                "scopes": tuple(provider.scopes),
                "resource": url,
                "operation": str(cfg.get("tool") or CONNECT_OPERATION),
            },
            connector=str(cfg.get("name", "")),
            owner=str(cfg["credential_owner"]),
        )
        record = await resolve_credential(request, ports)
        token = record.get("access_token") or record.get("access")
        if not isinstance(token, str) or not token:
            raise CredentialRefused(CredentialRefusal.MISSING)
        return {"Authorization": f"Bearer {token}"}


def bind_credential_reuse(
    fleet: Any,
    invocation: Any,
    *,
    vault: Any = None,
    providers: Any = None,
) -> bool:
    """Bind the fleet's reuse ports from the served invocation; ``False`` without an engine.

    ``invocation.decide_ask`` is the engine ``Decide`` seam of the host, ``vault`` the host
    secret store and ``providers(provider_id)`` the declared provider block.
    """
    ask = getattr(invocation, "decide_ask", None)
    if ask is None:
        return False
    from arbora.access.host_ports import credential_ports_for_host
    from arbora.fleet.credential_bridge import BrokerPkceBridge

    if vault is None:
        from arbora.security.secrets import create_secrets_client

        vault = create_secrets_client()
    if providers is None:
        providers = _declared_provider
    bridge = BrokerPkceBridge(fleet.oauth_brokers, providers)
    fleet.credentials.bind(credential_ports_for_host(ask, vault, broker=bridge))
    return True


def _declared_provider(provider_id: str) -> Mapping[str, Any] | None:
    from arbora.core.settings import runtime_config

    entries = runtime_config.remote_oauth_providers or ()
    for entry in entries:
        if isinstance(entry, Mapping) and entry.get("provider_id") == provider_id:
            return entry
    return None
