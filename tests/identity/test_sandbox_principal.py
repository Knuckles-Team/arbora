"""Sandbox principals and the second-tenant fixture (ARBORA-IDENTITY-R004.10)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from arbora.deployment import sandbox
from arbora.identity.sandbox_principal import (
    SandboxPrincipal,
    SandboxPrincipalRefusal,
    connect_kwargs,
    mint_sandbox_principals,
)
from arbora.mcp_server import composition


def _refusal(**kwargs: Any) -> str:
    with pytest.raises(SandboxPrincipalRefusal) as caught:
        mint_sandbox_principals(**kwargs)
    return caught.value.code


@pytest.mark.spec("ARBORA-IDENTITY-R004.10")
def test_mint_is_sandbox_only_loopback_only_and_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (one,) = mint_sandbox_principals(
        [("viewer", "sandbox-a")], profile="sandbox", bind_host="127.0.0.1"
    )
    assert (one.role, one.tenant) == ("viewer", "sandbox-a")
    assert one.admission == {"principal": one.principal, "tenant": "sandbox-a"}
    pair = [("operator", "sandbox-a")]
    assert (
        _refusal(roles=pair, profile="production", bind_host="127.0.0.1")
        == "SANDBOX_PRINCIPAL_OUTSIDE_PROFILE"
    )
    for host in ("0.0.0.0", "example.test", ""):  # noqa: S104 - the refused address
        assert (
            _refusal(roles=pair, profile="sandbox", bind_host=host)
            == "SANDBOX_PRINCIPAL_NOT_LOOPBACK"
        )
    for roles in ([], pair * 3, pair * 2, [("operator", "tenant-x")]):
        assert (
            _refusal(roles=roles, profile="sandbox", bind_host="::1")
            == "SANDBOX_PRINCIPAL_DUPLICATE"
        )

    # up mints and starts the engine in no-auth mode; down removes the store.
    started: list[composition.LocalEngineConfig] = []

    def fake_engine(
        config: composition.LocalEngineConfig,
    ) -> composition.EngineEndpoint:
        started.append(config)
        return config.endpoint

    monkeypatch.setattr(composition, "ensure_local_engine", fake_engine)
    args = argparse.Namespace(
        root=str(tmp_path), principal="operator", second_tenant_principal=None, port=0
    )
    summary = sandbox.up(args, hold=False, launcher=lambda *_: 0)
    assert started[0].extra_args == ("--auth-mode", "none")
    assert started[0].endpoint.address.startswith("127.0.0.1:")
    assert [p["tenant"] for p in summary["principals"]] == ["sandbox-a"]
    store = started[0].persist_dir
    assert store is not None and store.is_dir()
    with pytest.raises(SandboxPrincipalRefusal):
        sandbox.up(args, hold=False, launcher=lambda *_: 0)
    assert sandbox.down(args, terminate=lambda pid: None) is True
    assert not store.exists() and not (tmp_path / sandbox.STATE_FILE).exists()
    assert sandbox.down(args, terminate=lambda pid: None) is False


def test_two_tenant_fixture_holds_distinct_tenants_and_envelopes(
    two_sandbox_tenants: tuple[SandboxPrincipal, SandboxPrincipal],
) -> None:
    first, second = two_sandbox_tenants
    assert first.tenant != second.tenant and first.principal != second.principal
    assert connect_kwargs(first, _sandbox_connect)["sandbox"] == first.admission
    assert "sandbox-b" not in repr(first) and "sandbox-a" not in repr(second)
    with pytest.raises(SandboxPrincipalRefusal) as dup:
        mint_sandbox_principals(
            [("operator", "sandbox-a"), ("viewer", "sandbox-a")],
            profile="sandbox",
            bind_host="127.0.0.1",
        )
    assert dup.value.code == "SANDBOX_PRINCIPAL_DUPLICATE"


def _sandbox_connect(*, auth_mode: str, sandbox: dict[str, str]) -> None: ...


def _pinned_connect(*, auth_mode: str) -> None: ...


def test_a_client_without_sandbox_admission_refuses_unavailable(
    two_sandbox_tenants: tuple[SandboxPrincipal, SandboxPrincipal],
) -> None:
    with pytest.raises(SandboxPrincipalRefusal) as caught:
        connect_kwargs(two_sandbox_tenants[0], _pinned_connect)
    assert caught.value.code == "UNAVAILABLE"


class _TenantStore:
    """An engine double that resolves the tenant from the connecting envelope only.

    The envelope is the one the client sends; there is no tenant argument on a read, so a
    caller cannot name another tenant. A foreign object and an absent one answer alike.
    """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], str] = {}

    def read(self, envelope: dict[str, str], object_id: str) -> dict[str, Any]:
        found = self.objects.get((envelope["tenant"], object_id))
        return {"found": found is not None, "value": found}


@pytest.mark.spec("ARBORA-IDENTITY-R004.10.1")
def test_second_tenant_principal_cannot_read_the_first_tenants_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[composition.LocalEngineConfig] = []

    def fake_engine(
        config: composition.LocalEngineConfig,
    ) -> composition.EngineEndpoint:
        started.append(config)
        return config.endpoint

    monkeypatch.setattr(composition, "ensure_local_engine", fake_engine)
    monkeypatch.setattr(composition, "stand_down_engine", lambda endpoint: None)
    args = argparse.Namespace(
        root=str(tmp_path),
        principal="operator",
        second_tenant_principal="viewer",
        port=0,
    )
    summary = sandbox.up(args, hold=False, launcher=lambda *_: 0)
    # Exactly two principals, each in its own tenant, each with its own role.
    assert [(p["role"], p["tenant"]) for p in summary["principals"]] == [
        ("operator", "sandbox-a"),
        ("viewer", "sandbox-b"),
    ]
    first, second = (
        SandboxPrincipal(p["principal"], p["tenant"], p["role"])
        for p in summary["principals"]
    )
    assert first.principal != second.principal
    envelopes = [
        connect_kwargs(first, _sandbox_connect)["sandbox"],
        connect_kwargs(second, _sandbox_connect)["sandbox"],
    ]
    # Distinct admissions and no credential in either: nothing durable to leak.
    assert envelopes[0] != envelopes[1]
    assert set(envelopes[0]) == {"principal", "tenant"}

    store = _TenantStore()
    store.objects[("sandbox-a", "object-1")] = "first tenant's value"
    owned = store.read(envelopes[0], "object-1")
    foreign = store.read(envelopes[1], "object-1")
    absent = store.read(envelopes[1], "object-never-written")
    assert owned == {"found": True, "value": "first tenant's value"}
    assert foreign == absent == {"found": False, "value": None}

    # A repeated tenant is a duplicate, and the mint stays sandbox-only.
    with pytest.raises(SandboxPrincipalRefusal) as repeated:
        mint_sandbox_principals(
            [("operator", "sandbox-b"), ("viewer", "sandbox-b")],
            profile="sandbox",
            bind_host="127.0.0.1",
        )
    assert repeated.value.code == "SANDBOX_PRINCIPAL_DUPLICATE"
    with pytest.raises(SandboxPrincipalRefusal) as outside:
        mint_sandbox_principals(
            [("operator", "sandbox-a"), ("viewer", "sandbox-b")],
            profile="production",
            bind_host="127.0.0.1",
        )
    assert outside.value.code == "SANDBOX_PRINCIPAL_OUTSIDE_PROFILE"

    # down destroys the store, and with it both principals.
    store_dir = started[0].persist_dir
    assert store_dir is not None and store_dir.is_dir()
    assert sandbox.down(args, terminate=lambda pid: None) is True
    assert not store_dir.exists() and not (tmp_path / sandbox.STATE_FILE).exists()
