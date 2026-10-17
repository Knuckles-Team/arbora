"""The secrets client resolves bounded references over any backend."""

from __future__ import annotations

import pytest

from arbora.security.secrets import SecretsBackend, SecretsClient


class _Memory(SecretsBackend):
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, **metadata: object) -> None:
        self.data[key] = value

    def delete(self, key: str) -> bool:
        return self.data.pop(key, None) is not None

    def list_keys(self) -> list[str]:
        return sorted(self.data)


@pytest.fixture
def client() -> SecretsClient:
    return SecretsClient(backend=_Memory())


def test_store_reference_and_env_reference(
    client: SecretsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.set("svc/token", "v1")
    monkeypatch.setenv("G2_SECRET", "from-env")
    assert client.resolve_ref("vault://svc/token") == "v1"
    assert client.resolve_ref("env://G2_SECRET") == "from-env"


@pytest.mark.parametrize(
    "ref", ["", "plain", "ftp://x", "vault://../x", "vault://a b", "env://1BAD"]
)
def test_invalid_references_refuse(client: SecretsClient, ref: str) -> None:
    with pytest.raises(ValueError):
        client.resolve_ref(ref)


def test_set_if_absent_and_compare_and_set(client: SecretsClient) -> None:
    assert client.set_if_absent("k", "a") is True
    assert client.set_if_absent("k", "b") is False
    assert client.compare_and_set("k", "x", "c") is False
    assert client.compare_and_set("k", "a", "c") is True
    assert client.get("k") == "c"


def test_vault_sync_reports_present_written_missing(client: SecretsClient) -> None:
    client.set("svc/A", "1")
    result = client.vault_sync("svc", ["A", "B", "C"], values={"B": "2"})
    assert (result["present"], result["written"], result["missing"]) == (
        ["A"],
        ["B"],
        ["C"],
    )
    assert result["refs"]["C"] == "vault://svc/C"
