"""Arbora is the sole authenticated composition root for SDK runners."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from scion_sdk.credentials.references import SecretReference
from scion_sdk.runner.descriptors import RunnerSettings
from scion_sdk.sinks.medulla import PackImportAuthorityResolver

from arbora.connector_runner import (
    RELEASE_LOCK_MISSING,
    ConnectorRunnerComposition,
    ConnectorRunnerConfig,
    ConnectorRunnerNotReadyError,
    configured_release_lock,
)
from arbora.epistemic import AuthorityError, ClientContext, ConnectClient


class Resolver:
    def __init__(self, value: str = "engine-secret") -> None:
        self.value = value
        self.seen: list[SecretReference] = []

    def resolve(self, reference: SecretReference) -> str:
        self.seen.append(reference)
        return self.value


class Client:
    def __init__(self) -> None:
        self.contexts: list[dict[str, Any]] = []
        self.closed = False

    @contextmanager
    def use_verified_context(self, context: dict[str, Any]):
        self.contexts.append(context)
        yield

    async def close(self) -> None:
        self.closed = True


class Sink:
    def __init__(self, *, ready: bool = True, reason: str | None = None) -> None:
        self._readiness = SimpleNamespace(ready=ready, reason=reason)

    async def readiness(self) -> Any:
        return self._readiness


def _lock(tmp_path: Path) -> Path:
    lock = tmp_path / "connector-release.lock"
    lock.write_text("{}", encoding="utf-8")
    return lock


def config(tmp_path: Path, **overrides: object) -> ConnectorRunnerConfig:
    values: dict[str, object] = {
        "graph": "tenant-a:connectors",
        "state_dir": tmp_path,
        "auth_secret_ref": "env://GRAPH_SERVICE_AUTH_SECRET",
        "socket_path": "/run/medulla.sock",
    }
    values["release_lock"] = _lock(tmp_path)
    values.update(overrides)
    return ConnectorRunnerConfig(**values)  # type: ignore[arg-type]


def context(*, scopes: tuple[str, ...] = ("source:ingest",)) -> ClientContext:
    return ClientContext(
        principal="service:arbora",
        tenant="tenant-a",
        audience="medulla",
        agent_id="service:arbora",
        roles=("connector-runner",),
        scopes=scopes,
        policy_version="policy-7",
    )


async def pack_import_authority(_connector: str) -> Any:
    raise AssertionError("the SDK invokes the resolver only during pack import")


PACK_AUTHORITY = cast(PackImportAuthorityResolver, pack_import_authority)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"socket_path": None}, "exactly one"),
        ({"tcp_addr": "eg.example.invalid:9737"}, "exactly one"),
        ({"auth_secret_ref": "literal-secret"}, "secret reference"),
    ],
)
def test_config_refuses_missing_ambiguous_or_literal_auth(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        config(tmp_path, **overrides)


def test_composition_requires_dynamic_pack_import_authority(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="pack_import_authority is required"):
        ConnectorRunnerComposition(
            config(tmp_path),
            credential_resolver=Resolver(),
            pack_import_authority=cast(PackImportAuthorityResolver, None),
        )


@pytest.mark.asyncio
async def test_injects_exact_authenticated_client_and_verified_context(
    tmp_path: Path,
) -> None:
    resolver = Resolver()
    connected: list[dict[str, Any]] = []
    client = Client()
    built: list[dict[str, Any]] = []

    async def connect(**kwargs: Any) -> Client:
        connected.append(kwargs)
        return client

    def services_factory(settings: RunnerSettings, **kwargs: Any) -> Any:
        built.append({"settings": settings, **kwargs})
        return SimpleNamespace(sink=Sink())

    composition = ConnectorRunnerComposition(
        config(tmp_path),
        credential_resolver=resolver,
        pack_import_authority=PACK_AUTHORITY,
        connect=cast(ConnectClient, connect),
        services_factory=services_factory,
    )
    settings = RunnerSettings(max_concurrency=2)
    verified = context()

    async with composition.services(verified, settings) as services:
        assert services.sink is not None

    assert resolver.seen == [
        SecretReference(scheme="env", target="GRAPH_SERVICE_AUTH_SECRET")
    ]
    assert connected == [
        {
            "graph_name": "tenant-a:connectors",
            "verified_context": verified.to_claims(),
            "socket_path": "/run/medulla.sock",
            "tcp_addr": None,
            "auth_secret": "engine-secret",
        }
    ]
    assert client.contexts == [verified.to_claims()]
    assert built == [
        {
            "settings": settings,
            "state_dir": tmp_path,
            "sink_name": "medulla",
            "sink_client": client,
            "pack_import_authority": PACK_AUTHORITY,
            "policy": None,
        }
    ]
    await composition.close()
    assert client.closed is True


@pytest.mark.asyncio
async def test_refuses_raw_claims_and_missing_ingest_scope_before_connect(
    tmp_path: Path,
) -> None:
    calls = 0

    async def connect(**_kwargs: Any) -> Client:
        nonlocal calls
        calls += 1
        return Client()

    composition = ConnectorRunnerComposition(
        config(tmp_path),
        credential_resolver=Resolver(),
        pack_import_authority=PACK_AUTHORITY,
        connect=cast(ConnectClient, connect),
    )
    with pytest.raises(TypeError, match="verified ClientContext"):
        async with composition.services(
            cast(ClientContext, context().to_claims()), RunnerSettings()
        ):
            pass
    with pytest.raises(AuthorityError, match="source:ingest"):
        async with composition.services(
            context(scopes=("graph:read",)), RunnerSettings()
        ):
            pass
    assert calls == 0


@pytest.mark.asyncio
async def test_refuses_unready_injected_sink_without_fallback(tmp_path: Path) -> None:
    async def connect(**_kwargs: Any) -> Client:
        return Client()

    def services_factory(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            sink=Sink(ready=False, reason="ConnectorPack contract unavailable")
        )

    composition = ConnectorRunnerComposition(
        config(tmp_path),
        credential_resolver=Resolver(),
        pack_import_authority=PACK_AUTHORITY,
        connect=cast(ConnectClient, connect),
        services_factory=services_factory,
    )
    with pytest.raises(
        ConnectorRunnerNotReadyError, match="ConnectorPack contract unavailable"
    ):
        async with composition.services(context(), RunnerSettings()):
            pass


@pytest.mark.asyncio
async def test_tenant_graph_binding_is_not_reused_across_tenants(
    tmp_path: Path,
) -> None:
    async def connect(**_kwargs: Any) -> Client:
        return Client()

    def services_factory(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(sink=Sink())

    composition = ConnectorRunnerComposition(
        config(tmp_path),
        credential_resolver=Resolver(),
        pack_import_authority=PACK_AUTHORITY,
        connect=cast(ConnectClient, connect),
        services_factory=services_factory,
    )
    async with composition.services(context(), RunnerSettings()):
        pass

    other = ClientContext(
        principal="service:arbora",
        tenant="tenant-b",
        audience="medulla",
        agent_id="service:arbora",
        roles=("connector-runner",),
        scopes=("source:ingest",),
        policy_version="policy-7",
    )
    with pytest.raises(AuthorityError, match="another tenant"):
        async with composition.services(other, RunnerSettings()):
            pass


@pytest.mark.spec("ARBORA-HOST-R037")
def test_missing_release_lock_fails_bootstrap(tmp_path: Path) -> None:
    connects = 0

    async def connect(**_kwargs: Any) -> Client:
        nonlocal connects
        connects += 1
        return Client()

    resolver = Resolver()
    with pytest.raises(ConnectorRunnerNotReadyError, match=RELEASE_LOCK_MISSING):
        ConnectorRunnerComposition(
            config(tmp_path, release_lock=tmp_path / "absent.lock"),
            credential_resolver=resolver,
            pack_import_authority=PACK_AUTHORITY,
            connect=cast(ConnectClient, connect),
        )
    assert connects == 0
    assert resolver.seen == []


@pytest.mark.spec("ARBORA-HOST-R037")
def test_release_lock_defaults_under_the_config_dir_and_honours_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(tmp_path))
    assert configured_release_lock(None) == tmp_path / "connector-release.lock"
    assert configured_release_lock({}) == tmp_path / "connector-release.lock"
    assert configured_release_lock({"release_lock": "/srv/x.lock"}) == Path(
        "/srv/x.lock"
    )


@pytest.mark.spec("ARBORA-HOST-R037")
@pytest.mark.asyncio
async def test_the_configured_lock_reaches_the_sdk_settings(tmp_path: Path) -> None:
    class LockedSettings:
        """Structural double for an SDK settings model that carries the lock."""

        model_fields = {"release_lock": object()}

        def __init__(self, release_lock: Path | None = None) -> None:
            self.release_lock = release_lock

        def model_copy(self, *, update: dict[str, Any]) -> LockedSettings:
            return LockedSettings(**update)

    seen: list[Any] = []

    async def connect(**_kwargs: Any) -> Client:
        return Client()

    def services_factory(settings: Any, **_kwargs: Any) -> Any:
        seen.append(settings)
        return SimpleNamespace(sink=Sink())

    cfg = config(tmp_path)
    composition = ConnectorRunnerComposition(
        cfg,
        credential_resolver=Resolver(),
        pack_import_authority=PACK_AUTHORITY,
        connect=cast(ConnectClient, connect),
        services_factory=services_factory,
    )
    async with composition.services(context(), cast(Any, LockedSettings())):
        pass
    assert seen[0].release_lock == cfg.release_lock
