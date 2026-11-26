"""``arbora clients enroll`` (ARBORA-IDENTITY-R030.1)."""

from __future__ import annotations

import asyncio
import io

import pytest

from arbora.identity.client_enrollment import ClientEnrollmentRefused, enroll_client
from arbora.identity.client_enrollment_cli import enroll_main


class _Provider:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.clients = set(existing or ())
        self.created: list[tuple[str, tuple[str, ...]]] = []

    async def client_exists(self, name: str) -> bool:
        return name in self.clients

    async def create_client(self, name, scopes) -> str:
        self.created.append((name, tuple(scopes)))
        return "s3cr3t-value"


class _Vault:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def set_if_absent(self, key: str, value: str, **_) -> bool:
        return self.data.setdefault(key, value) == value


@pytest.mark.spec("ARBORA-IDENTITY-R030.1")
def test_enroll_stores_secret_in_vault_and_prints_none() -> None:
    provider, vault, out, err = _Provider(), _Vault(), io.StringIO(), io.StringIO()
    events: list = []
    code = enroll_main(
        ["claude-code", "--scope", "kg:read", "--scope", "workstream:read"],
        provider=provider,
        secrets=vault,
        stdout=out,
        stderr=err,
    )
    assert code == 0
    assert out.getvalue().strip() == "openbao://arbora/clients/claude-code/secret"
    assert vault.data["arbora/clients/claude-code/secret"] == "s3cr3t-value"
    assert "s3cr3t-value" not in out.getvalue() + err.getvalue()
    assert provider.created == [("claude-code", ("kg:read", "workstream:read"))]
    result = asyncio.run(
        enroll_client(
            "other",
            ["kg:read"],
            provider=_Provider(),
            secrets=_Vault(),
            audit=events.append,
        )
    )
    assert result.secret_ref.startswith("openbao://") and len(events) == 1
    assert "s3cr3t-value" not in repr(events)


@pytest.mark.spec("ARBORA-IDENTITY-R030.1")
def test_existing_name_refuses_without_change() -> None:
    provider, vault = _Provider({"claude-code"}), _Vault()
    with pytest.raises(ClientEnrollmentRefused) as refused:
        asyncio.run(
            enroll_client(
                "claude-code",
                ["kg:read"],
                provider=provider,
                secrets=vault,
                audit=lambda e: None,
            )
        )
    assert refused.value.code == "CLIENT_EXISTS"
    assert provider.created == [] and vault.data == {}
