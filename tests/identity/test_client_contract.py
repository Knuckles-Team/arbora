"""Arbora's OAuth client contract and its reconciliation pass (ARBORA-IDENTITY-R030.4.1, .6)."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from arbora.identity import client_contract as cc

REALM = "fixture"
CONFIG = cc.ContractConfig(
    admin_url="https://idp.example",
    realm=REALM,
    admin_client_id="arbora-identity-admin",
    admin_client_secret_ref="env://FIXTURE_ADMIN_SECRET",  # sanitizer:ignore - reference, not a secret
    audience="agent-services",
)
BUILTIN_SCOPES = ("roles", "offline_access", "profile", "email")


class FakeRealm:
    """An in-memory realm admin interface; records every call."""

    def __init__(self, scopes: tuple[str, ...] = BUILTIN_SCOPES) -> None:
        self.clients: dict[str, dict[str, Any]] = {}
        self.scopes: dict[str, dict[str, Any]] = {}
        self.mappers: dict[str, list[dict[str, Any]]] = {}
        self.defaults: dict[str, set[str]] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        self._ids = 0
        for name in scopes:
            self._add_scope({"name": name})

    def _id(self) -> str:
        self._ids += 1
        return f"id-{self._ids}"

    def _add_scope(self, body: dict[str, Any]) -> None:
        sid = self._id()
        self.scopes[sid] = {**body, "id": sid}
        self.mappers[sid] = []

    def __call__(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        if self.down:
            raise cc.ContractFailure("admin_unreachable")
        self.calls.append((method, path))
        url = urlsplit(path)
        parts = url.path.split("/")[4:]  # after /admin/realms/<realm>
        assert url.path.startswith(f"/admin/realms/{REALM}/")
        if parts[0] == "client-scopes":
            if len(parts) == 1 and method == "GET":
                return 200, list(self.scopes.values())
            if len(parts) == 1 and method == "POST":
                self._add_scope(dict(body))
                return 201, None
            if parts[2:] == ["protocol-mappers", "models"]:
                if method == "GET":
                    return 200, list(self.mappers[parts[1]])
                self.mappers[parts[1]].append(dict(body))
                return 201, None
        if parts[0] == "clients":
            if len(parts) == 1 and method == "GET":
                wanted = parse_qs(url.query)["clientId"][0]
                return 200, [
                    c for c in self.clients.values() if c["clientId"] == wanted
                ]
            if len(parts) == 1 and method == "POST":
                cid = self._id()
                self.clients[cid] = {**body, "id": cid}
                self.defaults[cid] = set()
                return 201, None
            if len(parts) == 2 and method == "PUT":
                self.clients[parts[1]] = {**body, "id": parts[1]}
                return 204, None
            if parts[2:3] == ["default-client-scopes"]:
                if method == "GET":
                    names = self.defaults[parts[1]]
                    return 200, [{"name": n} for n in sorted(names)]
                scope = self.scopes[parts[3]]["name"]
                self.defaults[parts[1]].add(scope)
                return 204, None
        return 404, None

    def native(self) -> dict[str, Any]:
        return next(
            c for c in self.clients.values() if c["clientId"] == CONFIG.native_client_id
        )

    def delete_native(self) -> None:
        cid = self.native()["id"]
        del self.clients[cid]
        del self.defaults[cid]


def factory(realm: FakeRealm):
    return lambda config: cc.RealmAdmin(config, realm)


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.1")
def test_native_client_is_public_pkce_with_declared_scopes() -> None:
    contract = cc.build_contract(CONFIG)
    (native,) = contract.clients
    body = cc.client_representation(native)
    assert body["clientId"] == "arbora-native-mcp"
    assert body["publicClient"] is True
    assert body["standardFlowEnabled"] is True
    assert not any(
        body[k]
        for k in (
            "implicitFlowEnabled",
            "directAccessGrantsEnabled",
            "serviceAccountsEnabled",
        )
    )
    assert body["attributes"]["pkce.code.challenge.method"] == "S256"
    assert body["redirectUris"] == list(cc.DEFAULT_NATIVE_REDIRECT_URIS)
    assert set(native.default_scopes) >= {
        "mcp-fleet",
        "roles",
        "agent-services",
        "offline_access",
    }
    # mcp-fleet is shared: it must exist, but arbora adds no mapper to it.
    assert {s.name: s.audience for s in contract.scopes} == {
        "mcp-fleet": None,
        "agent-services": "agent-services",
    }
    mapper = cc.audience_mapper(contract.scopes[1])
    assert mapper["protocolMapper"] == "oidc-audience-mapper"
    assert mapper["config"]["included.custom.audience"] == "agent-services"


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.1")
def test_configuration_is_validated_and_only_loopback_redirects_are_accepted() -> None:
    raw = {
        "admin_url": "https://idp.example/",
        "realm": REALM,
        "admin_client_id": "a",
        "admin_client_secret_ref": "env://X",  # sanitizer:ignore - reference
        "native_redirect_uris": ["http://127.0.0.1:4000/cb"],
    }
    config = cc.ContractConfig.from_mapping(raw)
    assert (config.admin_url, config.native_redirect_uris) == (
        "https://idp.example",
        ("http://127.0.0.1:4000/cb",),
    )
    with pytest.raises(ValueError, match="admin_client_secret_ref"):
        cc.ContractConfig.from_mapping({**raw, "admin_client_secret_ref": ""})
    with pytest.raises(ValueError, match="loopback"):
        cc.ContractConfig.from_mapping(
            {**raw, "native_redirect_uris": ["https://evil.example/cb"]}
        )


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.1")
def test_render_is_idempotent_and_touches_only_declared_items() -> None:
    realm = FakeRealm()
    realm.clients["other"] = {
        "id": "other",
        "clientId": "someone-else",
        "publicClient": False,
    }
    contract = cc.build_contract(CONFIG)

    first = cc.reconcile_once(contract, cc.RealmAdmin(CONFIG, realm))
    assert first.ok and first.changed
    assert set(realm.defaults[realm.native()["id"]]) == set(cc.NATIVE_DEFAULT_SCOPES)
    for sid, scope in realm.scopes.items():
        expected = 1 if scope["name"] == cc.AUDIENCE_SCOPE else 0
        assert len(realm.mappers[sid]) == expected, scope["name"]

    realm.calls.clear()
    second = cc.reconcile_once(contract, cc.RealmAdmin(CONFIG, realm))
    assert second.ok and second.changed == ()
    assert {method for method, _ in realm.calls} == {"GET"}  # nothing to write
    assert realm.clients["other"] == {
        "id": "other",
        "clientId": "someone-else",
        "publicClient": False,
    }
    assert not any(method == "DELETE" for method, _ in realm.calls)

    # Drift in an owned field is repaired; foreign attributes are kept.
    native = realm.native()
    native["redirectUris"] = ["http://localhost:1/x"]
    native["attributes"]["extra"] = "kept"
    third = cc.reconcile_once(contract, cc.RealmAdmin(CONFIG, realm))
    assert third.ok and third.changed == (f"client:{CONFIG.native_client_id}",)
    assert realm.native()["redirectUris"] == list(cc.DEFAULT_NATIVE_REDIRECT_URIS)
    assert realm.native()["attributes"]["extra"] == "kept"


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.2")
def test_a_missing_builtin_scope_and_a_refused_admin_are_typed_failures() -> None:
    realm = FakeRealm(scopes=("roles", "profile", "email"))  # no offline_access
    result = cc.reconcile_once(cc.build_contract(CONFIG), cc.RealmAdmin(CONFIG, realm))
    assert (result.ok, result.code, result.items) == (
        False,
        "scope_missing",
        ("scope:offline_access",),
    )
    refused = cc.RealmAdmin(CONFIG, lambda m, p, b: (403, None))
    assert cc.reconcile_once(cc.build_contract(CONFIG), refused).code == "admin_refused"


def _jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.2")
def test_a_provider_wide_admin_token_is_refused() -> None:
    assert cc._token_is_too_broad(_jwt({"realm_access": {"roles": ["admin"]}}))
    assert cc._token_is_too_broad(_jwt({"realm_access": {"roles": ["create-realm"]}}))
    assert not cc._token_is_too_broad(
        _jwt({"resource_access": {"realm-management": {"roles": ["manage-clients"]}}})
    )


@pytest.mark.spec("ARBORA-IDENTITY-R030.4.6")
def test_reconcile_command_prints_state_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arbora import console

    monkeypatch.delenv(cc.CONFIG_KEY, raising=False)
    monkeypatch.setattr("sys.argv", ["arbora", "identity", "reconcile"])
    with pytest.raises(SystemExit) as exited:
        console.main()
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "not_configured"

    realm = FakeRealm()
    monkeypatch.setenv(
        cc.CONFIG_KEY,
        json.dumps(
            {
                "admin_url": CONFIG.admin_url,
                "realm": REALM,
                "admin_client_id": CONFIG.admin_client_id,
                "admin_client_secret_ref": CONFIG.admin_client_secret_ref,
                "audience": "agent-services",
            }
        ),
    )
    monkeypatch.setattr(cc, "default_admin_factory", factory(realm))
    original = cc.reconcile_now
    monkeypatch.setattr(
        cc,
        "reconcile_now",
        lambda config, admin_factory=None: original(config, factory(realm)),
    )
    with pytest.raises(SystemExit) as exited:
        console.main()
    assert exited.value.code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "converged"
    assert f"client:{CONFIG.native_client_id}" in printed["changed"]
    assert "secret" not in json.dumps(printed).lower()

    realm.down = True
    with pytest.raises(SystemExit) as exited:
        console.main()
    assert exited.value.code == 1
    assert json.loads(capsys.readouterr().out)["code"] == "admin_unreachable"
