"""The host-composed identity services: SCIM routes and the directory sync job.

``ARBORA-IDENTITY-R011.2.4`` (SCIM routes) and ``ARBORA-IDENTITY-R010.2.3.2`` (scheduled
directory sync) are built as ports; this module is their production caller. The composition
root gives it the engine administration port and a service-scoped runtime, and it answers the
objects the served host mounts and starts. Arbora keeps no account store: every effect is one
engine ``Identity`` request made as the host service identity.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.identity.directory_sync import (
    JOB_ID,
    Directory,
    SyncConfig,
    SyncRefused,
    directory_sync_config,
    run_sync,
)
from arbora.identity.engine import IdentityUnavailable
from arbora.identity.ldap import LdapBindConfig
from arbora.identity.scim import ScimServiceCredential, ScimUser
from arbora.identity.scim_routes import ScimPorts

__all__ = [
    "SCIM_CREDENTIALS_PATH",
    "DirectorySyncService",
    "EngineScimIdentity",
    "ServiceIdentityPort",
    "directory_sync_service",
    "scim_ports_from_store",
]

#: The secret-backend path of the SCIM service credentials: a JSON object of provider id to
#: bearer token. Each provider has its own token (ARBORA-IDENTITY-R011.1).
SCIM_CREDENTIALS_PATH = "arbora/identity/scim-credentials"
SERVICE_SCOPES = frozenset({"identity:admin"})
_log = logging.getLogger("arbora.audit.identity")


def _emit(event: Mapping[str, str]) -> None:
    _log.info("identity.service", extra={"audit": dict(event)})


class ServiceIdentityPort:
    """``IdentityAdminPort.identity`` as the host service identity of one tenant.

    The runtime mints the exact ``identity:admin`` service claims for the tenant, so the
    port takes no client or claims from a caller.
    """

    def __init__(
        self,
        admin: Any,
        runtime: Any,
        service_claims: Callable[[str, frozenset[str]], Awaitable[Mapping[str, Any]]],
        tenant: Callable[[], str],
    ) -> None:
        self._admin = admin
        self._runtime = runtime
        self._claims = service_claims
        self._tenant = tenant

    async def identity(
        self,
        client: Any,
        claims: Mapping[str, Any],
        family: str,
        op: str,
        request: Mapping[str, Any] | None = None,
        *,
        expect: str,
    ) -> Any:
        tenant = self._tenant()
        async with self._runtime.as_service(
            tenant, required_scopes=SERVICE_SCOPES
        ) as service_client:
            service_claims = await self._claims(tenant, SERVICE_SCOPES)
            return await self._admin.identity(
                service_client, service_claims, family, op, request, expect=expect
            )


class EngineScimIdentity:
    """The SCIM identity port over the engine ``Identity`` ``user`` family."""

    def __init__(self, port: Any) -> None:
        self._port = port

    async def create_user(self, user: ScimUser) -> str:
        principal = await self._port.identity(
            None,
            {},
            "user",
            "create",
            {"username": user.user_name, "roles": [], "kind": "human"},
            expect="principal",
        )
        value = principal if isinstance(principal, Mapping) else {}
        principal_id = value.get("principal_id") or value.get("id")
        if not isinstance(principal_id, str) or not principal_id:
            raise IdentityUnavailable("identity create answered no principal")
        if not user.active:
            await self.deactivate_user(principal_id)
        return principal_id

    async def update_user(self, user_id: str, user: ScimUser) -> None:
        if user.active:
            await self._set_status(user_id, "active")
        else:
            await self.deactivate_user(user_id)

    async def deactivate_user(self, user_id: str) -> None:
        """Disable and revoke every session; owned data is kept (ARBORA-IDENTITY-R011)."""
        await self._set_status(user_id, "disabled")
        await self._port.identity(
            None, {}, "session", "revoke_all", {"id": user_id}, expect="done"
        )

    async def _set_status(self, user_id: str, status: str) -> None:
        await self._port.identity(
            None,
            {},
            "user",
            "set_status",
            {"principal_id": user_id, "status": status},
            expect="done",
        )


def _delivered_credentials(raw: Any) -> dict[str, ScimServiceCredential]:
    """Bearer token to provider-bound credential from the delivered JSON, else empty."""
    try:
        providers = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(providers, dict):
        return {}
    return {
        token: ScimServiceCredential(provider_id=provider, token_id=provider)
        for provider, token in providers.items()
        if isinstance(provider, str) and isinstance(token, str) and provider and token
    }


def scim_ports_from_store(
    store: Any,
    port: Any,
    *,
    emit: Callable[[Mapping[str, str]], None] = _emit,
) -> ScimPorts | None:
    """The SCIM ports of the host, or ``None`` when no credential is delivered.

    ``store.get`` reads the credential object; an absent or malformed value mounts nothing,
    so the route answers 404 rather than an open endpoint.
    """
    credentials = _delivered_credentials(store.get(SCIM_CREDENTIALS_PATH))
    if not credentials:
        return None
    return ScimPorts(
        credentials=credentials, identity=EngineScimIdentity(port), emit=emit
    )


class DirectorySyncService:
    """The ``identity.directory_sync`` scheduler job (ARBORA-IDENTITY-R010.2.3.2).

    A background service: one :func:`run_sync` every ``interval_seconds`` of the injected
    clock. A refused run (bind failure, deprovision fraction) skips and keeps the prior
    state; the next tick tries again.
    """

    job_id = JOB_ID

    def __init__(
        self,
        port: Any,
        directory: Directory,
        config: SyncConfig,
        clock: Any,
        *,
        emit: Callable[[Mapping[str, str]], None] = _emit,
    ) -> None:
        self._port = port
        self._directory = directory
        self._config = config
        self._clock = clock
        self._emit = emit

    async def tick(self) -> bool:
        """One run; ``True`` when it applied, ``False`` when it was refused."""
        try:
            await run_sync(
                self._port,
                None,
                {},
                self._directory,
                self._config,
                emit=self._emit,
            )
        except (SyncRefused, IdentityUnavailable):
            return False
        return True

    async def run(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            await self.tick()
            await self._clock.sleep(self._config.interval_seconds)


def directory_sync_service(
    read: Callable[..., Any],
    port: Any,
    clock: Any,
    directory_factory: Callable[[], Directory],
    *,
    emit: Callable[[Mapping[str, str]], None] = _emit,
) -> DirectorySyncService | None:
    """The job when the deployment configures LDAPS, else ``None``.

    ``read(name, default)`` is the deployment settings reader. The group-to-role mapping is a
    JSON object of group DN to role names.
    """
    host = str(read("ARBORA_LDAP_HOST", "") or "").strip()
    if not host:
        return None
    mapping = json.loads(str(read("ARBORA_LDAP_GROUP_ROLES", "{}") or "{}"))
    bind = LdapBindConfig(
        host=host,
        port=int(str(read("ARBORA_LDAP_PORT", "636") or "636")),
        bind_dn=str(read("ARBORA_LDAP_BIND_DN", "") or ""),
    )
    block = {
        "sync_interval_seconds": int(
            str(read("ARBORA_LDAP_SYNC_INTERVAL_S", "900") or "900")
        ),
        "max_deprovision_fraction": float(
            str(read("ARBORA_LDAP_MAX_DEPROVISION_FRACTION", "0.2") or "0.2")
        ),
    }
    config = directory_sync_config({"identity": {"ldap": block}}, bind, mapping)
    return DirectorySyncService(port, directory_factory(), config, clock, emit=emit)
