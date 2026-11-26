"""``arbora deploy provision`` and its sibling commands (ARBORA-DEPLOY-R602.2)."""

from __future__ import annotations

import ast
import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from arbora.deployment import provision

DEPLOY = {
    "namespace": "apps",
    "workload": "arbora",
    "secret_store": {"mount": "kv", "path": "arbora/engine"},
    "identity": {
        "realm_url": "https://sso.example.test/realms/main",
        "process_client": "arbora",
        "admin_secret": "ops/idp-admin",
        "webui_client": "arbora-ui",
        "webui_redirects": ["https://arbora.example.test/*"],
        "ca_bundle": "/etc/ssl/trust/ca-bundle.pem",
    },
    "tenant": "main",
    "principals": [
        {
            "name": "cli",
            "kind": "client",
            "ref": "cli-client",
            "roles": ["arbora-admin"],
        },
        {
            "name": "operator",
            "kind": "username",
            "ref": "admin",
            "roles": ["arbora-admin"],
        },
        {"name": "worker", "kind": "service", "ref": "service:worker"},
    ],
    "config_path": "config.json",
    "registry": "registry.example.test",
    "public_url": "https://arbora.example.test/",
}


def _jwt(claims: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class FakeIdp:
    """An in-memory identity admin API."""

    def __init__(self) -> None:
        self.roles: set[str] = set()
        self.mapped: dict[str, set[str]] = {
            "u-proc": set(),
            "u-cli": set(),
            "u-admin": set(),
        }
        self.calls: list[tuple[str, str]] = []
        self.webui = {"id": "ui", "redirectUris": [], "attributes": {}}

    def __call__(
        self, path: str, method: str = "GET", body: object | None = None
    ) -> Any:
        self.calls.append((method, path))
        if path.startswith("/clients?clientId="):
            cid = path.split("=", 1)[1]
            return [self.webui] if cid == "arbora-ui" else [{"id": f"c-{cid}"}]
        if path.startswith("/clients/c-") and path.endswith("service-account-user"):
            return {
                "id": "u-proc" if "c-arbora/" in path else "u-cli",
                "username": "svc",
            }
        if path.startswith("/clients/ui") and method == "PUT":
            assert isinstance(body, dict)
            self.webui = body
            return None
        if path.startswith("/users?username="):
            return [{"id": "u-admin"}]
        if path.endswith("/role-mappings/realm") and method == "GET":
            user = path.split("/")[2]
            return [{"name": r} for r in sorted(self.mapped[user])]
        if path.endswith("/role-mappings/realm") and method == "POST":
            assert isinstance(body, list)
            self.mapped[path.split("/")[2]] |= {r["name"] for r in body}
            return None
        if path.startswith("/roles?"):
            return [{"name": r} for r in sorted(self.roles)]
        if path == "/roles" and method == "POST":
            assert isinstance(body, dict)
            self.roles.add(body["name"])
            return None
        if path.startswith("/roles/"):
            return {"name": path.rsplit("/", 1)[1].replace("%3A", ":")}
        raise AssertionError(f"unexpected admin call {method} {path}")


def _ports(idp: FakeIdp, signers: dict[str, Any] | None = None) -> provision.Ports:
    secrets_by_name = {
        "arbora-secrets": {
            "OIDC_TOKEN_URL": "t",
            "OIDC_CLIENT_ID": "a",
            "OIDC_CLIENT_SECRET": "s",
        },
        "epistemic-graph-secrets": {
            "EPISTEMIC_GRAPH_SIGNER_KEYS_JSON": json.dumps(
                signers or {"proc-sub": {"key": "k", "may_grant_system": True}}
            ),
            "EPISTEMIC_GRAPH_AUDIENCE": "aud",
            "EPISTEMIC_GRAPH_POLICY_VERSION": "7",
            "GRAPH_SERVICE_AUTH_SECRET": "x",
            "EPISTEMIC_GRAPH_TENANT": "main",
            "EPISTEMIC_GRAPH_TXN_RECOVERY_KEY": "r",
        },
        "epistemic-kvcache-secrets": {"EPISTEMIC_GRAPH_KVCACHE_TOKEN": "c"},
    }
    return provision.Ports(
        read_secret=lambda ns, name: secrets_by_name[name],
        read_config=lambda ns, name: {
            "AUTH_JWT_AUDIENCE": "aud",
            "KG_POLICY_VERSION": "7",
        },
        form_post=lambda url, data: {
            "access_token": _jwt(
                {"sub": "proc-sub", "roles": list(provision.PROCESS_ROLES)}
            )
        },
        admin=idp,
        run=lambda argv, stdin: b"",
    )


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "deploy.yml"
    path.write_text(yaml.safe_dump(DEPLOY))
    return path


@pytest.mark.spec("ARBORA-DEPLOY-R602.2")
def test_each_subcommand_reads_only_the_deploy_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("HOME", "KUBECONFIG", "OIDC_TOKEN_URL", "ARBORA_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"z": 1}))
    DEPLOY["config_path"] = str(tmp_path / "config.json")
    config_path = _write_config(tmp_path)
    config = provision.DeployConfig.load(config_path)
    assert (
        config.namespace == "apps" and config.graphs[1] == "tenant__main____commons__"
    )
    # A deploy file with an unknown field is refused with its field path.
    bad: dict[str, Any] = {**DEPLOY, "identity": {**DEPLOY["identity"], "stray": "x"}}  # type: ignore[dict-item]
    with pytest.raises(
        provision.ConfigError, match="identity has unknown fields: stray"
    ):
        provision.DeployConfig.from_mapping(bad)
    # Only the pod payload functions touch os.environ; no command function does.
    tree = ast.parse(Path(provision.__file__).read_text())
    pod_side = {"_pod_enrol"}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name not in pod_side:
            source = ast.unparse(node)
            assert "os.environ" not in source and "os.getenv" not in source, node.name
    # The commands run from the file with every external effect injected.
    workdir = tmp_path / "work"
    idp = FakeIdp()
    ports = _ports(idp)
    assert provision.provision(config, ports, workdir, apply=True) == 0
    assert (workdir / "doc.json").stat().st_mode & 0o777 == 0o600
    assert idp.mapped["u-proc"] == set(provision.PROCESS_ROLES)
    assert idp.mapped["u-cli"] == {"arbora-admin"}
    assert idp.webui["redirectUris"] == ["https://arbora.example.test/*"]
    assert provision.main(["normalize-config", "--config", str(config_path)]) == 0


@pytest.mark.spec("ARBORA-DEPLOY-R602.2")
def test_provision_never_rewrites_an_existing_secret() -> None:
    desired = {"A": "1", "B": "2", "C": "3"}
    assert provision.secret_plan(desired, None) == ("create", desired, [])
    action, patch, drift = provision.secret_plan(desired, {"A": "1", "B": "changed"})
    assert action == "patch"
    assert patch == {"C": "3"}  # only the missing key is written
    assert drift == ["B"]  # a different existing value is reported, never overwritten
    assert provision.secret_plan(desired, {"A": "1", "B": "2", "C": "3"}) == (
        "in-sync",
        {},
        [],
    )


@pytest.mark.spec("ARBORA-DEPLOY-R602.2")
def test_secret_patch_writes_only_missing_keys(tmp_path: Path) -> None:
    (tmp_path / "doc.json").write_text(json.dumps({"A": "1", "B": "2"}))
    (tmp_path / "store-current.json").write_text(
        json.dumps({"data": {"data": {"A": "other"}}})
    )
    assert provision.secret_patch(tmp_path) == 0
    assert json.loads((tmp_path / "store-patch.json").read_text()) == {"B": "2"}


class FakeConsensus:
    def __init__(self, identities: dict[str, dict[str, Any]]) -> None:
        self.identities = identities
        self.registered: list[tuple[str, list[str]]] = []

    async def get_identity(self, agent_id: str) -> dict[str, Any] | None:
        return self.identities.get(agent_id)

    async def register_identity(
        self, agent_id: str, kind: str, caps: list[str], roles: list[str], **_: Any
    ) -> None:
        self.registered.append((agent_id, roles))
        self.identities[agent_id] = {"roles": roles}

    async def check_access(self, agent_id: str, action: str, graph: str) -> bool:
        return "tenant:main" in self.identities[agent_id]["roles"]


class FakeClient:
    def __init__(self, identities: dict[str, dict[str, Any]]) -> None:
        self.consensus = FakeConsensus(identities)


@pytest.mark.spec("ARBORA-DEPLOY-R602.2")
def test_enrol_adds_to_the_current_role_set(capsys: pytest.CaptureFixture[str]) -> None:
    spec = {
        "tenant_role": "tenant:main",
        "graphs": ["__commons__"],
        "principals": [
            {"name": "new", "agent_id": "a-new"},
            {"name": "has-other", "agent_id": "a-other"},
            {"name": "done", "agent_id": "a-done"},
            {"name": "proc", "agent_id": "proc"},
        ],
    }
    client = FakeClient(
        {"a-other": {"roles": ["reader"]}, "a-done": {"roles": ["tenant:main", "x"]}}
    )
    assert asyncio.run(provision.enrol_on_engine(spec, client, "proc", "key")) == 0
    registered = dict(client.consensus.registered)
    assert registered["a-new"] == ["tenant:main"]
    assert registered["a-other"] == [
        "reader",
        "tenant:main",
    ]  # the union, never a replacement
    assert "a-done" not in registered  # already held: not registered again
    assert "SKIP proc is the process principal" in capsys.readouterr().out
    # The command resolves subjects from the file's principals and runs the pod payload.
    idp = FakeIdp()
    spec_out = provision.enrolment_spec(
        provision.DeployConfig.from_mapping(DEPLOY), _ports(idp)
    )
    assert {p["name"]: p["agent_id"] for p in spec_out["principals"]} == {
        "cli": "u-cli",
        "operator": "u-admin",
        "worker": "service:worker",
    }
    sent: list[list[str]] = []
    ports = _ports(idp)
    ports.run = lambda argv, stdin: sent.append(argv) or b"PASS\n"  # type: ignore[func-returns-value]
    assert provision.enrol(provision.DeployConfig.from_mapping(DEPLOY), ports) == 0
    assert (
        sent[0][:6] == ["kubectl", "-n", "apps", "exec", "-i", "deploy/arbora"]
        and "enrol-apply" in sent[0]
    )


@pytest.mark.spec("ARBORA-DEPLOY-R602.2")
def test_normalize_probe_and_pod_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = {"a": None, "b": [1], "X_TOKEN": "", "Y_SECRET": "env://Y", "z": 1}
    new, report = provision.normalize(document)
    assert new == {"b": "[1]", "Y_SECRET": "env://Y", "z": 1}
    assert report["removed_null"] == ["a"] and report["removed_empty_credential"] == [
        "X_TOKEN"
    ]
    assert provision.normalize({"K_TOKEN": "plain"})[1]["invalid"] == ["K_TOKEN"]

    # The in-pod checks run against an injected loopback and report each result.
    def http(
        path: str,
        body: object,
        extra: dict[str, str] | None = None,
        timeout: float = 300,
    ) -> tuple[int, dict[str, str], bytes]:
        if path == "/mcp":
            method = body["method"]  # type: ignore[index]
            if method == "initialize":
                return 200, {}, json.dumps({"result": {}}).encode()
            if method == "tools/list":
                return (
                    200,
                    {},
                    json.dumps(
                        {"result": {"tools": [{"name": "find"}, {"name": "ask"}]}}
                    ).encode(),
                )
            return (
                200,
                {},
                json.dumps({"result": {"structuredContent": {"ok": 1}}}).encode(),
            )
        return 200, {}, json.dumps({"value": {"name": "list_things"}}).encode()

    checks = provision.PodChecks({"fleet_checks": [["github", ["get_me"]]]}, http)
    checks.verify()
    assert checks.failed == []
    assert "PASS mcp.tools/list" in capsys.readouterr().out
