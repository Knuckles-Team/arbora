"""The declared identity file is one idempotent reconcile (ARBORA-DEPLOY-R603)."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from arbora.identity import client_contract as cc
from arbora.identity import declaration as dl
from arbora.identity import declaration_reconcile as dr

REALM = "fixture"
BASE = f"/admin/realms/{REALM}"
TRUSTED = "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"

FILE: dict[str, Any] = {
    "version": 1,
    "realm": REALM,
    "admin": {
        "url": "https://idp.example",
        "client_id": "admin-client",
        "secret_ref": "env://FIXTURE_ADMIN_SECRET",  # sanitizer:ignore - reference
    },
    "settings": {"bruteForceProtected": True, "failureFactor": 10},
    "roles": [
        {"name": "reader"},
        {
            "name": "operator",
            "description": "reader plus write",
            "composites": ["reader"],
        },
        {"name": "site-admin"},
    ],
    "groups": ["home", "business"],
    "scopes": [
        {"name": "agent-services", "audience": "agent-services"},
        {
            "name": "groups",
            "realm_default": True,
            "attach_to_all_clients": True,
            "mappers": [
                {
                    "name": "groups",
                    "protocolMapper": "oidc-group-membership-mapper",
                    "config": {"claim.name": "groups"},
                }
            ],
        },
    ],
    "dcr": {"trusted_hosts": ["localhost", "127.0.0.1"]},
    "clients": [
        {
            "clientId": "arbora-native-mcp",
            "kind": "public",
            "name": "Native MCP clients",
            "redirectUris": ["http://localhost:33418/callback"],
            "postLogoutRedirectUris": ["+"],
            "default_scopes": ["agent-services", "groups"],
        },
        {
            "clientId": "grafana",
            "kind": "confidential",
            "name": "Grafana",
            "rootUrl": "https://grafana.example",
            "redirectUris": ["https://grafana.example/login"],
            "webOrigins": ["https://grafana.example"],
            "pkce": "S256",
            "mappers": [
                {
                    "name": "roles in groups",
                    "protocolMapper": "oidc-usermodel-realm-role-mapper",
                    "config": {"claim.name": "groups"},
                }
            ],
            "secret_target": {
                "path": "grafana",
                "id_key": "ID",
                "secret_key": "SECRET",
            },
        },
        {
            "clientId": "agent",
            "kind": "service",
            "service_account": {
                "email": "agent@example.test",
                "first_name": "Agent",
                "roles": ["operator"],
                "forbidden_roles": ["site-admin"],
            },
        },
    ],
    "users": [
        {
            "username": "owner",
            "email": "owner@example.test",
            "first_name": "Owner",
            "groups": ["home"],
            "roles": ["site-admin"],
        }
    ],
}


class FakeIdp:
    """An in-memory realm admin interface covering the calls the reconcile makes."""

    def __init__(self) -> None:
        self.realm: dict[str, Any] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.composites: dict[str, list[str]] = {}
        self.groups: dict[str, str] = {}
        self.scopes: dict[str, dict[str, Any]] = {}
        self.scope_mappers: dict[str, list[dict[str, Any]]] = {}
        self.scope_roles: dict[str, list[str]] = {}
        self.realm_defaults: set[str] = set()
        self.clients: dict[str, dict[str, Any]] = {}
        self.client_scopes: dict[str, set[str]] = {}
        self.client_mappers: dict[str, list[dict[str, Any]]] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.user_groups: dict[str, set[str]] = {}
        self.user_roles: dict[str, set[str]] = {}
        self.components = {
            "c1": {
                "id": "c1",
                "providerId": "trusted-hosts",
                "subType": "anonymous",
                "config": {"trusted-hosts": ["old.example"]},
            }
        }
        self.writes: list[tuple[str, str]] = []
        self._n = 0
        self.add_role("offline_access")

    def nid(self) -> str:
        self._n += 1
        return f"id{self._n}"

    def add_role(self, name: str) -> None:
        self.roles[name] = {"id": self.nid(), "name": name}

    def by_id(self, table: dict[str, dict[str, Any]], key: str) -> str:
        return next(k for k, v in table.items() if v["id"] == key)

    def __call__(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        url = urlsplit(path)
        assert url.path.startswith(BASE), path
        rel = url.path[len(BASE) :]
        query = parse_qs(url.query)
        if method != "GET":
            self.writes.append((method, rel))
        parts = [p for p in rel.split("/") if p]
        if not parts:
            return self._realm(method, [], query, body)
        handler = getattr(self, "_" + parts[0].replace("-", "_"))
        return handler(method, parts[1:], query, body)

    def _realm(self, method, parts, query, body):
        if method == "PUT":
            self.realm.update(body)
            return 204, None
        return 200, dict(self.realm)

    def _roles(self, method, parts, query, body):
        if not parts:
            if method == "POST":
                self.roles[body["name"]] = {**body, "id": self.nid()}
                return 201, None
            return 200, list(self.roles.values())
        name = parts[0]
        if len(parts) == 1:
            if method == "PUT":
                self.roles[name].update(body)
                return 204, None
            return (200, self.roles[name]) if name in self.roles else (404, None)
        if method == "POST":
            self.composites.setdefault(name, []).extend(r["name"] for r in body)
            return 204, None
        return 200, [self.roles[n] for n in self.composites.get(name, [])]

    def _groups(self, method, parts, query, body):
        if method == "POST":
            self.groups[body["name"]] = self.nid()
            return 201, None
        return 200, [{"name": n, "id": i} for n, i in self.groups.items()]

    def _client_scopes(self, method, parts, query, body):
        if not parts:
            if method == "POST":
                sid = self.nid()
                self.scopes[sid] = {**body, "id": sid}
                self.scope_mappers[sid] = []
                return 201, None
            return 200, list(self.scopes.values())
        sid = parts[0]
        if parts[1] == "protocol-mappers":
            if method == "POST":
                self.scope_mappers[sid].append(dict(body))
                return 201, None
            return 200, list(self.scope_mappers[sid])
        if method == "POST":
            self.scope_roles.setdefault(sid, []).extend(r["name"] for r in body)
            return 204, None
        return 200, [self.roles[n] for n in self.scope_roles.get(sid, [])]

    def _default_default_client_scopes(self, method, parts, query, body):
        if method == "PUT":
            self.realm_defaults.add(parts[0])
            return 204, None
        return 200, [self.scopes[s] for s in self.realm_defaults]

    def _components(self, method, parts, query, body):
        if method == "PUT":
            self.components[parts[0]] = body
            return 204, None
        return 200, list(self.components.values())

    def _clients(self, method, parts, query, body):
        if not parts:
            if method == "POST":
                cid = self.nid()
                self.clients[cid] = {**body, "id": cid}
                self.client_scopes[cid] = set()
                self.client_mappers[cid] = []
                return 201, None
            if "clientId" in query:
                return 200, [
                    c
                    for c in self.clients.values()
                    if c["clientId"] == query["clientId"][0]
                ]
            return 200, list(self.clients.values())
        cid = parts[0]
        if len(parts) == 1:
            self.clients[cid] = {**body, "id": cid}
            return 204, None
        sub = parts[1]
        if sub == "default-client-scopes":
            if method == "PUT":
                self.client_scopes[cid].add(parts[2])
                return 204, None
            return 200, [self.scopes[s] for s in self.client_scopes[cid]]
        if sub == "protocol-mappers":
            if method == "POST":
                self.client_mappers[cid].append({**body, "id": self.nid()})
                return 201, None
            return 200, list(self.client_mappers[cid])
        if sub == "service-account-user":
            key = f"sa-{cid}"
            if key not in self.users:
                self.users[key] = {"id": key, "username": key}
                self.user_groups[key] = set()
                self.user_roles[key] = set()
            return 200, self.users[key]
        if sub == "client-secret":
            return 200, {"value": f"secret-of-{cid}"}
        raise AssertionError(parts)

    def _users(self, method, parts, query, body):
        if not parts:
            if method == "POST":
                uid = self.nid()
                self.users[uid] = {**body, "id": uid}
                self.user_groups[uid] = set()
                self.user_roles[uid] = set()
                return 201, None
            if "username" in query:
                return 200, [
                    u
                    for u in self.users.values()
                    if u.get("username") == query["username"][0]
                ]
            return 200, [
                u for u in self.users.values() if u.get("email") == query["email"][0]
            ]
        uid = parts[0]
        if len(parts) == 1:
            self.users[uid].update(body)
            return 204, None
        if parts[1] == "groups":
            if method == "PUT":
                self.user_groups[uid].add(parts[2])
                return 204, None
            return 200, [
                {"name": n}
                for n, i in self.groups.items()
                if i in self.user_groups[uid]
            ]
        if method == "POST":
            self.user_roles[uid].update(r["name"] for r in body)
            return 204, None
        return 200, [{"name": n} for n in self.user_roles[uid]]


class Sink:
    def __init__(self) -> None:
        self.held: dict[str, dict[str, str]] = {}
        self.published: list[str] = []

    def __call__(self, target: dl.SecretTarget, values) -> bool:
        if self.held.get(target.path) == dict(values):
            return False
        self.held[target.path] = dict(values)
        self.published.append(target.path)
        return True


def _admin(idp: FakeIdp, decl: dl.Declaration) -> cc.RealmAdmin:
    return cc.RealmAdmin(decl.contract_config(), idp)


def _decl(**patch: Any) -> dl.Declaration:
    return dl.parse_declaration({**json.loads(json.dumps(FILE)), **patch})


@pytest.mark.spec("ARBORA-DEPLOY-R603")
def test_reconcile_is_idempotent_and_reports_drift() -> None:
    idp, sink, decl = FakeIdp(), Sink(), _decl()
    admin = _admin(idp, decl)

    drift = dr.reconcile(decl, admin, mode="check")
    assert drift.state == "drift" and drift.exit_code == 1
    assert not idp.writes, "check never writes"
    plan = dr.reconcile(decl, admin, mode="plan")
    assert plan.state == "planned" and "client:grafana" in plan.changed
    assert not idp.writes, "plan never writes"

    first = dr.reconcile(decl, admin, sink)
    assert first.state == "converged", first
    assert {
        "client:arbora-native-mcp",
        "client:grafana",
        "group:home",
        "role:reader",
    } <= set(first.changed)
    assert sink.published == ["grafana"]

    writes_after_first = len(idp.writes)
    second = dr.reconcile(decl, admin, sink)
    assert second.state == "converged" and second.changed == ()
    assert len(idp.writes) == writes_after_first, "a converged provider is not written"
    assert dr.reconcile(decl, admin, mode="check").state == "converged"

    state = next(c for c in idp.clients.values() if c["clientId"] == "grafana")
    state["redirectUris"] = ["https://elsewhere.example/login"]
    again = dr.reconcile(decl, admin, mode="check")
    assert again.state == "drift" and "client:grafana" in again.changed


def test_matching_native_client_is_never_rewritten() -> None:
    """The live native client, already matching, gets no write at all."""
    idp, decl = FakeIdp(), _decl()
    admin = _admin(idp, decl)
    dr.reconcile(decl, admin, Sink())
    native = next(
        c for c in idp.clients.values() if c["clientId"] == "arbora-native-mcp"
    )
    before = json.dumps(native, sort_keys=True)
    idp.writes.clear()
    dr.reconcile(decl, admin, Sink())
    assert json.dumps(native, sort_keys=True) == before
    assert not [w for w in idp.writes if native["id"] in w[1]]
    assert native["publicClient"] is True
    assert native["attributes"]["pkce.code.challenge.method"] == "S256"
    assert native == {
        **cc.client_representation(
            cc.ClientSpec(
                "arbora-native-mcp",
                "Native MCP clients",
                ("http://localhost:33418/callback",),
                ("agent-services", "groups"),
            )
        ),
        "id": native["id"],
    }


def test_declared_effects_land_and_nothing_is_deleted() -> None:
    idp, decl = FakeIdp(), _decl()
    idp.add_role("unmanaged")
    stray = idp.nid()
    idp.clients[stray] = {
        "id": stray,
        "clientId": "stray",
        "protocol": "openid-connect",
    }
    idp.client_scopes[stray] = set()
    dr.reconcile(decl, _admin(idp, decl), Sink())
    assert idp.realm == {"bruteForceProtected": True, "failureFactor": 10}
    assert idp.composites["operator"] == ["reader"]
    assert idp.components["c1"]["config"]["trusted-hosts"] == ["localhost", "127.0.0.1"]
    assert set(idp.groups) == {"home", "business"}
    assert "unmanaged" in idp.roles and stray in idp.clients
    groups_scope = next(s["id"] for s in idp.scopes.values() if s["name"] == "groups")
    assert groups_scope in idp.realm_defaults
    for cid, attached in idp.client_scopes.items():
        assert groups_scope in attached, f"groups scope on every OIDC client ({cid})"
    agent = next(c for c in idp.clients.values() if c["clientId"] == "agent")
    account = idp.users[f"sa-{agent['id']}"]
    assert account["email"] == "agent@example.test"
    assert idp.user_roles[account["id"]] == {"operator"}
    owner = next(u for u in idp.users.values() if u.get("username") == "owner")
    assert owner["requiredActions"] == ["UPDATE_PASSWORD", "VERIFY_EMAIL"]
    assert idp.user_roles[owner["id"]] == {"site-admin"}


def test_forbidden_role_and_duplicate_email_are_refused() -> None:
    idp, decl = FakeIdp(), _decl()
    dr.reconcile(decl, _admin(idp, decl), Sink())
    agent = next(c for c in idp.clients.values() if c["clientId"] == "agent")
    idp.user_roles[f"sa-{agent['id']}"].add("site-admin")
    outcome = dr.reconcile(decl, _admin(idp, decl), Sink())
    assert outcome.state == "degraded"
    assert "forbidden-role:agent:site-admin" in outcome.violations

    other = FakeIdp()
    other.users["x"] = {"id": "x", "username": "someone", "email": "owner@example.test"}
    result = dr.reconcile(decl, _admin(other, decl), Sink())
    assert "user:owner:email-belongs-to-another-account" in result.violations


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"surprise": 1}, "unknown key"),
        ({"version": 2}, "version"),
        (
            {"users": [{"username": "u", "email": "e@x.test", "groups": ["nope"]}]},
            "undeclared group",
        ),
        ({"clients": [{"clientId": "p", "kind": "public"}]}, "redirectUris"),
        ({"clients": [{"clientId": "c", "kind": "weird"}]}, "kind"),
        ({"dcr": {"trusted_hosts": []}}, "trusted_hosts"),
    ],
)
def test_schema_is_closed(patch: dict[str, Any], message: str) -> None:
    with pytest.raises(dl.DeclarationError, match=message):
        _decl(**patch)


def test_declaration_holds_no_secret_value() -> None:
    """Credentials are references; a plain secret key is not in the schema."""
    with pytest.raises(dl.DeclarationError, match="unknown key"):
        _decl(admin={**FILE["admin"], "client_secret": "x"})


def test_command_reports_invalid_file(tmp_path, capsys) -> None:
    bad = tmp_path / "identity.json"
    bad.write_text('{"version": 9}')
    assert dr.declared_command(["--file", str(bad)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_file_loader_reads_yaml_and_json(tmp_path) -> None:
    import yaml

    yml, js = tmp_path / "a.yml", tmp_path / "a.json"
    yml.write_text(yaml.safe_dump(FILE))
    js.write_text(json.dumps(FILE))
    assert dl.load_declaration(yml) == dl.load_declaration(js)
