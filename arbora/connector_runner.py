"""Authenticated composition of scion-sdk runners.

Arbora owns deployment identity and the medulla transport.  The SDK
owns connector/source behavior and runner construction.  This module joins
those public contracts without manufacturing identity, copying an EG DTO, or
letting the SDK discover credentials from ambient process state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from medulla import (
    MedullaClient,
    RequestContextClaims,
    validate_request_context,
)
from scion_sdk.credentials.references import parse_secret_reference
from scion_sdk.credentials.resolution import resolve_secret_reference
from scion_sdk.credentials.resolver import CredentialResolver
from scion_sdk.discovery import ActivationPolicy
from scion_sdk.runner.composition import default_services
from scion_sdk.runner.descriptors import RunnerSettings
from scion_sdk.runner.services import RunnerServices
from scion_sdk.sinks.medulla import PackImportAuthorityResolver

from arbora.core.settings import config_dir
from arbora.epistemic import ClientContext, ConnectClient, EpistemicClientPool

__all__ = [
    "ConnectorRunnerComposition",
    "ConnectorRunnerConfig",
    "ConnectorRunnerNotReadyError",
    "RELEASE_LOCK_DEFAULT_NAME",
    "RELEASE_LOCK_MISSING",
    "SOURCE_INGEST_SCOPE",
    "configured_release_lock",
]

SOURCE_INGEST_SCOPE = "source:ingest"
RELEASE_LOCK_MISSING = "RELEASE_LOCK_MISSING"
RELEASE_LOCK_DEFAULT_NAME = "connector-release.lock"


def configured_release_lock(section: Mapping[str, object] | None = None) -> Path:
    """The release-lock path: ``connector_runner.release_lock`` or the default.

    ``section`` is the ``connector_runner`` mapping of ``arbora.yml``. The
    default is ``connector-release.lock`` in the Arbora config directory. No
    environment variable or fallback disables release verification.
    """
    raw = (section or {}).get("release_lock")
    if raw is None or not str(raw).strip():
        return config_dir() / RELEASE_LOCK_DEFAULT_NAME
    return Path(str(raw)).expanduser()


class ConnectorRunnerNotReadyError(RuntimeError):
    """The authenticated connector runner cannot safely accept work."""


@dataclass(frozen=True, slots=True)
class ConnectorRunnerConfig:
    """Reference-only deployment configuration for one connector runner.

    Exactly one transport endpoint is required.  ``auth_secret_ref`` is parsed
    on construction but resolved only while Arbora composes the live client;
    secret material is never part of this durable value.
    """

    graph: str
    state_dir: Path
    auth_secret_ref: str
    socket_path: str | None = None
    tcp_addr: str | None = None
    release_lock: Path = field(default_factory=configured_release_lock)

    def __post_init__(self) -> None:
        if not self.graph.strip():
            raise ValueError("connector runner graph must be a non-empty string")
        if not isinstance(self.state_dir, Path):
            raise TypeError("connector runner state_dir must be a Path")
        if bool(self.socket_path) == bool(self.tcp_addr):
            raise ValueError("connector runner requires exactly one medulla endpoint")
        for name in ("socket_path", "tcp_addr"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"connector runner {name} must be non-empty")
        _require_path(self.release_lock)
        parse_secret_reference(self.auth_secret_ref)


def _require_path(value: object) -> None:
    if not isinstance(value, Path):
        raise TypeError("connector runner release_lock must be a Path")


def _require_release_lock(lock: Path) -> None:
    """Release verification has no off switch: bootstrap stops before any
    secret is resolved or any client is created."""
    if not lock.is_file():
        raise ConnectorRunnerNotReadyError(
            f"{RELEASE_LOCK_MISSING}: create the release lock with "
            "create_release_key_lock at connector_runner.release_lock"
        )


ServicesFactory = Callable[..., RunnerServices]
_DEFAULT_CONNECT = cast(ConnectClient, MedullaClient.connect)


class ConnectorRunnerComposition:
    """Own the EG transport pool and inject its verified client into the SDK.

    The caller supplies a :class:`ClientContext` previously derived from a
    verified Arbora actor.  Raw claim mappings are deliberately not accepted.
    One composition instance owns one client pool and must be closed at process
    shutdown.
    """

    def __init__(
        self,
        config: ConnectorRunnerConfig,
        *,
        credential_resolver: CredentialResolver,
        pack_import_authority: PackImportAuthorityResolver,
        connect: ConnectClient = _DEFAULT_CONNECT,
        services_factory: ServicesFactory = default_services,
    ) -> None:
        _require_release_lock(config.release_lock)
        if credential_resolver is None:
            raise TypeError("credential_resolver is required")
        if not callable(pack_import_authority):
            raise TypeError("pack_import_authority is required")
        auth_secret = resolve_secret_reference(
            config.auth_secret_ref, credential_resolver
        )
        if not auth_secret:
            raise ConnectorRunnerNotReadyError(
                "medulla authentication secret resolved empty"
            )
        self._config = config
        self._pack_import_authority = pack_import_authority
        self._services_factory = services_factory
        self._pool = EpistemicClientPool(
            connect,
            socket_path=config.socket_path,
            tcp_addr=config.tcp_addr,
            auth_secret=auth_secret,
        )

    @staticmethod
    def _verified_claims(context: ClientContext) -> RequestContextClaims:
        if not isinstance(context, ClientContext):
            raise TypeError("connector runner requires a verified ClientContext")
        context.require_scope(SOURCE_INGEST_SCOPE)
        return validate_request_context(context.to_claims())

    def _with_release_lock(self, settings: RunnerSettings) -> RunnerSettings:
        """Set the SDK's required ``release_lock`` on a settings copy.

        A scion-sdk release that predates the field carries no lock to set; the
        composition still refuses a missing lock at construction.
        """
        if "release_lock" not in type(settings).model_fields:
            return settings
        return settings.model_copy(update={"release_lock": self._config.release_lock})

    @asynccontextmanager
    async def services(
        self,
        context: ClientContext,
        settings: RunnerSettings,
        *,
        policy: ActivationPolicy | None = None,
    ) -> AsyncIterator[RunnerServices]:
        """Yield SDK services bound to one authenticated EG client.

        Readiness is checked before the services escape the composition root.
        There is no unauthenticated, ``client=None``, alternate-sink, or ambient
        identity path.
        """

        self._verified_claims(context)
        async with self._pool.bind(
            context,
            self._config.graph,
            required_scope=SOURCE_INGEST_SCOPE,
        ) as client:
            services = self._services_factory(
                self._with_release_lock(settings),
                state_dir=self._config.state_dir,
                sink_name="medulla",
                sink_client=client,
                pack_import_authority=self._pack_import_authority,
                policy=policy,
            )
            readiness = await services.sink.readiness()
            if not readiness.ready:
                reason = readiness.reason or "medulla sink is not ready"
                raise ConnectorRunnerNotReadyError(reason)
            yield services

    async def close(self) -> None:
        """Close every graph-bound EG client owned by this composition."""

        await self._pool.close()
