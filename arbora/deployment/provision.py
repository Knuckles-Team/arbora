"""Provisioning commands of ``arbora deploy`` (ARBORA-DEPLOY-R602.2).

Five subcommands, each reading one closed-schema ``deploy.yml`` and no
environment variable except the path of that file:

* ``provision``        engine admission document, secret-store patch plan and
                       identity roles; creates what is missing and never
                       rewrites an existing secret value;
* ``enrol``            enrol client principals on the engine; reads the current
                       role set first and adds, because the engine's
                       ``RegisterIdentity`` replaces a role set;
* ``normalize-config`` flatten a legacy config file to scalars and references;
* ``probe-sso``        browser-path single sign-on probe of the web UI;
* ``check-in-pod``     checks run inside the pod, with this module as the
                       payload of ``kubectl exec``.

Every subcommand prints key names, hashes, booleans and role names only, never
a credential, key or token. The module is dependency-free (standard library
and, lazily, PyYAML) so it can be the stdin payload of a pod.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import html
import http.cookiejar
import json
import os
import re
import secrets
import ssl
import subprocess  # nosec B404 - fixed kubectl argv, no shell
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from arbora.core.environment import inherited_environment
from arbora.core.settings import RuntimeConfig

#: The realm roles the process token must carry.
PROCESS_ROLES = (
    "kg:admin",
    "connector:catalog-attest",
    "admin:connector-pack",
    "agent:pack-control",
    "security:admin",
    "mcp:discover",
    "mcp:delegate",
    "query:unified",
    "node:read",
    "ingest:read",
    "ingest:write",
    "query:nl",
    "source:ingest",
    "kv:read",
    "kv:write",
    "kv:shared:read",
    "kv:shared:write",
)
CREDENTIAL_SUFFIXES = ("_SECRET", "_PASSWORD", "_TOKEN", "_API_KEY", "_PRIVATE_KEY")
REFERENCE_SCHEMES = ("env://", "openbao://")
PRINCIPAL_KINDS = ("client", "username", "service")
PROBE_USER = "arbora-sso-probe"


class ConfigError(ValueError):
    """The deploy file is not valid; the message names the field path."""


# -- the deploy file --------------------------------------------------------


def _closed(
    value: Any,
    field: str,
    required: set[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be an object")
    unknown = sorted(set(value) - required - set(optional))
    if unknown:
        raise ConfigError(f"{field} has unknown fields: {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        raise ConfigError(f"{field} is missing: {', '.join(missing)}")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _texts(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list")
    return tuple(_text(item, f"{field}[{i}]") for i, item in enumerate(value))


@dataclasses.dataclass(frozen=True)
class Principal:
    name: str
    kind: str
    ref: str
    roles: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class DeployConfig:
    """The validated ``deploy.yml``: every environment value a command needs."""

    namespace: str
    workload: str
    secret_mount: str
    secret_path: str
    realm_url: str
    process_client: str
    admin_secret: str
    webui_client: str
    webui_redirects: tuple[str, ...]
    ca_bundle: str
    public_url: str
    tenant: str
    principals: tuple[Principal, ...]
    config_path: str
    registry: str
    host: str
    fleet_checks: tuple[tuple[str, tuple[str, ...]], ...]
    topology: str = ""
    contract: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> DeployConfig:
        doc = _closed(
            dict(raw),
            "deploy",
            {
                "namespace",
                "workload",
                "secret_store",
                "identity",
                "tenant",
                "principals",
                "config_path",
                "registry",
                "public_url",
            },
            {"host", "fleet_checks", "topology", "contract"},
        )
        store = _closed(doc["secret_store"], "secret_store", {"mount", "path"})
        ident = _closed(
            doc["identity"],
            "identity",
            {
                "realm_url",
                "process_client",
                "admin_secret",
                "webui_client",
                "webui_redirects",
                "ca_bundle",
            },
        )
        principals = []
        if not isinstance(doc["principals"], list):
            raise ConfigError("principals must be a list")
        for index, item in enumerate(doc["principals"]):
            entry = _closed(
                item, f"principals[{index}]", {"name", "kind", "ref"}, {"roles"}
            )
            if entry["kind"] not in PRINCIPAL_KINDS:
                raise ConfigError(
                    f"principals[{index}].kind must be one of {', '.join(PRINCIPAL_KINDS)}"
                )
            principals.append(
                Principal(
                    _text(entry["name"], f"principals[{index}].name"),
                    entry["kind"],
                    _text(entry["ref"], f"principals[{index}].ref"),
                    _texts(entry.get("roles", []), f"principals[{index}].roles"),
                )
            )
        checks = []
        for index, item in enumerate(doc.get("fleet_checks", [])):
            entry = _closed(item, f"fleet_checks[{index}]", {"server"}, {"preferred"})
            checks.append(
                (
                    _text(entry["server"], f"fleet_checks[{index}].server"),
                    _texts(
                        entry.get("preferred", []), f"fleet_checks[{index}].preferred"
                    ),
                )
            )
        admin_secret = _text(ident["admin_secret"], "identity.admin_secret")
        if "/" not in admin_secret:
            raise ConfigError("identity.admin_secret must be <namespace>/<secret name>")
        return cls(
            namespace=_text(doc["namespace"], "namespace"),
            workload=_text(doc["workload"], "workload"),
            secret_mount=_text(store["mount"], "secret_store.mount"),
            secret_path=_text(store["path"], "secret_store.path"),
            realm_url=_text(ident["realm_url"], "identity.realm_url").rstrip("/"),
            process_client=_text(ident["process_client"], "identity.process_client"),
            admin_secret=admin_secret,
            webui_client=_text(ident["webui_client"], "identity.webui_client"),
            webui_redirects=_texts(
                ident["webui_redirects"], "identity.webui_redirects"
            ),
            ca_bundle=_text(ident["ca_bundle"], "identity.ca_bundle"),
            public_url=_text(doc["public_url"], "public_url"),
            tenant=_text(doc["tenant"], "tenant"),
            principals=tuple(principals),
            config_path=_text(doc["config_path"], "config_path"),
            registry=_text(doc["registry"], "registry"),
            host=str(
                doc.get("host")
                or urllib.parse.urlsplit(_text(doc["public_url"], "public_url")).netloc
            ),
            fleet_checks=tuple(checks),
            topology=str(doc.get("topology", "")),
            contract=str(doc.get("contract", "")),
        )

    @classmethod
    def load(cls, path: Path) -> DeployConfig:
        import yaml

        try:
            return cls.from_mapping(
                yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            )
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(f"cannot read deploy file {path}: {exc}") from exc

    def to_mapping(self) -> dict[str, Any]:
        """The file's own shape, for the pod payload."""
        return {
            "namespace": self.namespace,
            "workload": self.workload,
            "secret_store": {"mount": self.secret_mount, "path": self.secret_path},
            "identity": {
                "realm_url": self.realm_url,
                "process_client": self.process_client,
                "admin_secret": self.admin_secret,
                "webui_client": self.webui_client,
                "webui_redirects": list(self.webui_redirects),
                "ca_bundle": self.ca_bundle,
            },
            "tenant": self.tenant,
            "topology": self.topology,
            "contract": self.contract,
            "principals": [
                {"name": p.name, "kind": p.kind, "ref": p.ref, "roles": list(p.roles)}
                for p in self.principals
            ],
            "config_path": self.config_path,
            "registry": self.registry,
            "public_url": self.public_url,
            "host": self.host,
            "fleet_checks": [
                {"server": s, "preferred": list(p)} for s, p in self.fleet_checks
            ],
        }

    @property
    def graphs(self) -> tuple[str, ...]:
        return ("__commons__", f"tenant__{self.tenant}____commons__")


# -- ports ------------------------------------------------------------------


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _claims(token: str) -> dict[str, Any]:
    segment = token.split(".")[1]
    return dict(
        json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    )


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)


@dataclasses.dataclass
class Ports:
    """Everything a command touches outside its own process, replaceable in tests."""

    read_secret: Callable[[str, str], dict[str, str]]
    read_config: Callable[[str, str], dict[str, str]]
    form_post: Callable[[str, dict[str, str]], dict[str, Any]]
    admin: Callable[..., Any]
    run: Callable[[list[str], bytes | None], bytes]

    @classmethod
    def real(cls, config: DeployConfig) -> Ports:
        context = ssl.create_default_context(cafile=config.ca_bundle)

        def read_secret(namespace: str, name: str) -> dict[str, str]:
            raw = subprocess.check_output(  # nosec B603 B607 - fixed kubectl argv
                ["kubectl", "-n", namespace, "get", "secret", name, "-o", "json"]
            )  # noqa: S603,S607
            return {
                k: base64.b64decode(v).decode()
                for k, v in json.loads(raw)["data"].items()
            }

        def read_config(namespace: str, name: str) -> dict[str, str]:
            raw = subprocess.check_output(  # nosec B603 B607 - fixed kubectl argv
                ["kubectl", "-n", namespace, "get", "configmap", name, "-o", "json"]
            )  # noqa: S603,S607
            return dict(json.loads(raw).get("data") or {})

        def form_post(url: str, data: dict[str, str]) -> dict[str, Any]:
            request = urllib.request.Request(url, urllib.parse.urlencode(data).encode())  # noqa: S310
            with urllib.request.urlopen(
                request, context=context, timeout=30
            ) as response:  # noqa: S310  # nosec B310
                return dict(json.load(response))

        def run(argv: list[str], stdin: bytes | None) -> bytes:
            return subprocess.run(  # nosec B603 - argv is built by this module
                argv, input=stdin, check=True, capture_output=True
            ).stdout  # noqa: S603

        ports = cls(read_secret, read_config, form_post, lambda *a, **k: None, run)
        namespace, name = config.admin_secret.split("/", 1)
        ports.admin = _keycloak_admin(config, ports, namespace, name, context)
        return ports


def _keycloak_admin(
    config: DeployConfig,
    ports: Ports,
    namespace: str,
    name: str,
    context: ssl.SSLContext,
) -> Callable[..., Any]:
    """An admin-API caller whose token is minted on first use."""
    state: dict[str, str] = {}
    base, realm = config.realm_url.rsplit("/realms/", 1)

    def call(path: str, method: str = "GET", body: object | None = None) -> Any:
        if "token" not in state:
            secret = ports.read_secret(namespace, name)
            state["token"] = ports.form_post(
                f"{config.realm_url}/protocol/openid-connect/token",
                {
                    "grant_type": "client_credentials",
                    "client_id": secret["KEYCLOAK_CLIENT_ID"],
                    "client_secret": secret["KEYCLOAK_CLIENT_SECRET"],
                },
            )["access_token"]
        request = urllib.request.Request(  # noqa: S310
            f"{base}/admin/realms/{realm}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {state['token']}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, context=context, timeout=30) as response:  # noqa: S310  # nosec B310
            raw = response.read()
            return json.loads(raw) if raw else None

    return call


# -- provision ---------------------------------------------------------------


def process_token(config: DeployConfig, ports: Ports) -> str:
    secret = ports.read_secret(config.namespace, f"{config.workload}-secrets")
    token = ports.form_post(
        secret["OIDC_TOKEN_URL"],
        {
            "grant_type": "client_credentials",
            "client_id": secret["OIDC_CLIENT_ID"],
            "client_secret": secret["OIDC_CLIENT_SECRET"],
        },
    )["access_token"]
    return str(token)


def _held(claims: Mapping[str, Any]) -> set[str]:
    return (
        set(claims.get("roles", []))
        | set(claims.get("realm_access", {}).get("roles", []))
        | set(str(claims.get("scope", "")).split())
    )


def build_doc(config: DeployConfig, ports: Ports, workdir: Path) -> int:
    """Write the engine admission document into ``workdir`` (mode 600)."""
    engine = ports.read_secret(config.namespace, "epistemic-graph-secrets")
    cache = ports.read_secret(config.namespace, "epistemic-kvcache-secrets")
    environment = ports.read_config(config.namespace, f"{config.workload}-env")
    principal = _claims(process_token(config, ports))["sub"]
    signers = json.loads(engine["EPISTEMIC_GRAPH_SIGNER_KEYS_JSON"])
    if principal not in signers:
        print("process principal is a registered signer: False")
        return 1
    entry = signers[principal]
    checks = {
        "process principal is a registered signer": True,
        "signer may_grant_system": entry.get("may_grant_system") is True,
        "audience matches the environment": engine["EPISTEMIC_GRAPH_AUDIENCE"]
        == environment["AUTH_JWT_AUDIENCE"],
        "policy version matches the environment": engine[
            "EPISTEMIC_GRAPH_POLICY_VERSION"
        ]
        == environment["KG_POLICY_VERSION"],
    }
    for name, value in checks.items():
        print(f"{name}: {value}")
    if not all(checks.values()):
        return 1
    document = {
        "GRAPH_SERVICE_AUTH_SECRET": engine["GRAPH_SERVICE_AUTH_SECRET"],
        "EPISTEMIC_GRAPH_SIGNER_KEYS_JSON": json.dumps({principal: entry}),
        "EPISTEMIC_GRAPH_TENANT": engine["EPISTEMIC_GRAPH_TENANT"],
        "EPISTEMIC_GRAPH_CONNECTOR_PACK_IMPORTERS": "*=principal:sha256:"
        + _sha(principal),
        "EPISTEMIC_GRAPH_TXN_RECOVERY_KEY": engine["EPISTEMIC_GRAPH_TXN_RECOVERY_KEY"],
        "EPISTEMIC_GRAPH_KVCACHE_TOKEN": cache["EPISTEMIC_GRAPH_KVCACHE_TOKEN"],
        "GRAPH_OS_PROCESS_PRINCIPAL": principal,
    }
    _write_private(workdir / "doc.json", json.dumps(document))
    print("document keys:", sorted(document), "| signers: 1")
    return 0


def secret_plan(
    desired: Mapping[str, str], current: Mapping[str, str] | None
) -> tuple[str, dict[str, str], list[str]]:
    """Plan a secret-store write: ``(action, keys to add, keys that differ)``.

    Only keys absent from the store are added. A key that exists with another
    value is reported as drift and left untouched: patch, never put.
    """
    if not current:
        return "create", dict(desired), []
    missing = {key: value for key, value in desired.items() if key not in current}
    drift = sorted(
        key
        for key, value in desired.items()
        if key in current and _sha(str(current[key])) != _sha(value)
    )
    return ("patch" if missing else "in-sync"), missing, drift


def secret_patch(workdir: Path) -> int:
    """Read ``doc.json`` and ``store-current.json`` from ``workdir``; write ``store-patch.json``."""
    desired = json.loads((workdir / "doc.json").read_text())
    current_path = workdir / "store-current.json"
    current = (
        json.loads(current_path.read_text())["data"]["data"]
        if current_path.exists() and current_path.read_text().strip()
        else None
    )
    action, patch, drift = secret_plan(desired, current)
    _write_private(workdir / "store-patch.json", json.dumps(patch))
    if drift:
        print("keys present with a different value (left untouched):", drift)
    extra = sorted(set(current or {}) - set(desired))
    if extra:
        print("keys present in the store but not desired (left untouched):", extra)
    print(f"{action} {len(patch)}" if patch else action, sorted(patch) if patch else "")
    return 0


def _map_missing_roles(
    admin: Callable[..., Any],
    user_id: str,
    wanted: tuple[str, ...],
    *,
    create: bool,
    apply: bool,
) -> tuple[list[str], bool]:
    mapped = {r["name"] for r in admin(f"/users/{user_id}/role-mappings/realm")}
    missing = [r for r in wanted if r not in mapped]
    if missing and apply:
        if create:
            existing = {r["name"] for r in admin("/roles?max=1000")}
            for name in missing:
                if name not in existing:
                    admin(
                        "/roles",
                        "POST",
                        {"name": name, "description": "arbora process scope"},
                    )
        reps = [
            admin(f"/roles/{urllib.parse.quote(name, safe='')}") for name in missing
        ]
        admin(f"/users/{user_id}/role-mappings/realm", "POST", reps)
    after = (
        {r["name"] for r in admin(f"/users/{user_id}/role-mappings/realm")}
        if apply
        else mapped
    )
    return missing, mapped <= after and (not apply or all(r in after for r in wanted))


def keycloak_roles(config: DeployConfig, ports: Ports, *, apply: bool) -> int:
    """The web UI client accepts its hosts and the process account carries every role (add-only)."""
    admin = ports.admin
    client = admin(f"/clients?clientId={config.webui_client}")[0]
    redirects = list(client.get("redirectUris") or [])
    attrs = dict(client.get("attributes") or {})
    logout = [
        u for u in str(attrs.get("post.logout.redirect.uris") or "").split("##") if u
    ]
    add_redirect = [u for u in config.webui_redirects if u not in redirects]
    add_logout = [u for u in config.webui_redirects if u not in logout]
    if add_redirect or add_logout:
        if not apply:
            print(f"{config.webui_client} missing:", add_redirect + add_logout)
            return 1
        client["redirectUris"] = redirects + add_redirect
        attrs["post.logout.redirect.uris"] = "##".join(logout + add_logout)
        client["attributes"] = attrs
        admin(f"/clients/{client['id']}", "PUT", client)
        print(f"{config.webui_client}: added", add_redirect + add_logout)
    else:
        print(f"{config.webui_client}: redirect and post-logout URIs present")
    process = admin(f"/clients?clientId={config.process_client}")[0]
    user = admin(f"/clients/{process['id']}/service-account-user")
    missing, ok = _map_missing_roles(
        admin, user["id"], PROCESS_ROLES, create=True, apply=apply
    )
    print(
        "service account:",
        user["username"],
        f"| mapped of {len(PROCESS_ROLES)}:",
        len(PROCESS_ROLES) - len(missing),
    )
    if missing and not apply:
        print("keycloak roles missing:", missing)
        return 1
    return 0 if ok else 1


def token_check(config: DeployConfig, ports: Ports, workdir: Path) -> int:
    claims = _claims(process_token(config, ports))
    principal = json.loads((workdir / "doc.json").read_text())[
        "GRAPH_OS_PROCESS_PRINCIPAL"
    ]
    missing = [r for r in PROCESS_ROLES if r not in _held(claims)]
    print("token sub is the process principal:", claims.get("sub") == principal)
    print("token missing roles:", missing or "none")
    return 0 if claims.get("sub") == principal and not missing else 1


def client_roles(config: DeployConfig, ports: Ports, *, apply: bool = True) -> int:
    """Each principal's configured realm roles, add-only."""
    admin, failed = ports.admin, 0
    for principal in config.principals:
        if not principal.roles or principal.kind == "service":
            continue
        user_id = _subject(admin, principal)
        missing, ok = _map_missing_roles(
            admin, user_id, principal.roles, create=False, apply=apply
        )
        failed += not ok
        print(
            f"{principal.name}: added {missing or 'none'}; holds all {len(principal.roles)}: {ok}"
        )
    return 1 if failed else 0


def _subject(admin: Callable[..., Any], principal: Principal) -> str:
    """The identity-provider subject a principal's token carries."""
    if principal.kind == "service":
        return principal.ref
    if principal.kind == "client":
        client = admin(f"/clients?clientId={principal.ref}")[0]
        return str(admin(f"/clients/{client['id']}/service-account-user")["id"])
    users = admin(f"/users?username={urllib.parse.quote(principal.ref)}&exact=true")
    if len(users) != 1:
        raise ConfigError(f"{len(users)} identity users named {principal.ref!r}")
    return str(users[0]["id"])


def enrolment_spec(config: DeployConfig, ports: Ports) -> dict[str, Any]:
    return {
        "tenant_role": f"tenant:{config.tenant}",
        "graphs": list(config.graphs),
        "principals": [
            {"name": p.name, "agent_id": _subject(ports.admin, p)}
            for p in config.principals
        ],
    }


def provision(
    config: DeployConfig,
    ports: Ports,
    workdir: Path,
    *,
    apply: bool,
) -> int:
    """Build the admission document, plan the secret patch, check or map roles, check the token."""
    workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for step in (
        lambda: build_doc(config, ports, workdir),
        lambda: secret_patch(workdir),
        lambda: keycloak_roles(config, ports, apply=apply),
        lambda: client_roles(config, ports, apply=apply),
        lambda: token_check(config, ports, workdir),
    ):
        code = step()
        if code:
            return code
    return 0


# -- enrol -------------------------------------------------------------------


async def enrol_on_engine(
    spec: Mapping[str, Any], client: Any, process: str, signer_key: str
) -> int:
    """Register each principal, adding the tenant role to the current role set.

    ``RegisterIdentity`` replaces a role set, so the current set is read first
    and the registration carries the union; a principal that already holds the
    role is not registered again.
    """
    role, failed = spec["tenant_role"], 0
    for item in spec["principals"]:
        name, agent_id = item["name"], item["agent_id"]
        if agent_id == process:
            print(f"SKIP {name} is the process principal")
            continue
        current = await client.consensus.get_identity(agent_id)
        held = sorted((current or {}).get("roles") or [])
        if current is None or role not in held:
            await client.consensus.register_identity(
                agent_id,
                "Agent",
                [],
                sorted({*held, role}),
                signer_id=process,
                signer_key=signer_key,
            )
            current = await client.consensus.get_identity(agent_id)
            action = "registered" if not held else "role added to the current set"
        else:
            action = "already holds the role (not re-registered)"
        roles = sorted((current or {}).get("roles") or [])
        access = {
            graph: await client.consensus.check_access(agent_id, "read", graph=graph)
            for graph in spec["graphs"]
        }
        ok = (
            current is not None
            and role in roles
            and set(held) <= set(roles)
            and all(access.values())
        )
        failed += not ok
        print(
            f"{'PASS' if ok else 'FAIL'} {name} {action}; roles={roles} read={access}",
            flush=True,
        )
    return 1 if failed else 0


def enrol(config: DeployConfig, ports: Ports) -> int:
    """Resolve the principals, then run the enrolment inside the pod with this module as its payload."""
    spec = enrolment_spec(config, ports)
    return _in_pod(config, ports, ["enrol-apply", json.dumps(spec)])


def _in_pod(config: DeployConfig, ports: Ports, arguments: list[str]) -> int:
    argv = [
        "kubectl",
        "-n",
        config.namespace,
        "exec",
        "-i",
        f"deploy/{config.workload}",
        "-c",
        config.workload,
        "--",
        "python",
        "-I",
        "-",
        *arguments,
    ]
    try:
        output = ports.run(argv, Path(__file__).read_bytes())
    except subprocess.CalledProcessError as exc:
        sys.stdout.write((exc.stdout or b"").decode(errors="replace"))
        return exc.returncode or 1
    sys.stdout.write(output.decode(errors="replace"))
    return 0


def _pod_enrol(spec_json: str) -> int:
    import asyncio

    from epistemic_graph import EpistemicGraphClient

    environment = inherited_environment()
    signers = json.loads(environment["EPISTEMIC_GRAPH_SIGNER_KEYS_JSON"])
    if len(signers) != 1:
        print(
            f"FAIL signer the pod holds {len(signers)} signers, expected the process principal only"
        )
        return 1
    process, entry = next(iter(signers.items()))

    async def run() -> int:
        client = await EpistemicGraphClient.connect(
            socket_path=environment.get(
                "GRAPH_SERVICE_SOCKET", "/run/epistemic-graph/epistemic-graph.sock"
            ),
            auth_secret=environment["GRAPH_SERVICE_AUTH_SECRET"],
            graph_name="__commons__",
            verified_context={
                "principal": process,
                "tenant": environment["EPISTEMIC_GRAPH_TENANT"],
                "audience": environment["EPISTEMIC_GRAPH_AUDIENCE"],
                "agent_id": process,
                "roles": [],
                "scopes": ["security:admin", "security:check"],
                "policy_version": environment["EPISTEMIC_GRAPH_POLICY_VERSION"],
                "delegation": [],
            },
        )
        try:
            return await enrol_on_engine(
                json.loads(spec_json), client, process, entry["key"]
            )
        finally:
            await client.close()

    return asyncio.run(run())


# -- normalize-config --------------------------------------------------------


def normalize(
    document: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Null removed; an empty credential removed; list and dict as compact JSON."""
    out: dict[str, Any] = {}
    report: dict[str, list[str]] = {
        "removed_null": [],
        "removed_empty_credential": [],
        "json_encoded": [],
        "invalid": [],
    }
    for key, value in document.items():
        if value is None:
            report["removed_null"].append(key)
            continue
        if isinstance(value, (list, dict)):
            value = json.dumps(value, separators=(",", ":"))
            report["json_encoded"].append(key)
        if key.upper().endswith(CREDENTIAL_SUFFIXES):
            rendered = str(value)
            if rendered == "":
                report["removed_empty_credential"].append(key)
                continue
            if not rendered.startswith(REFERENCE_SCHEMES):
                report["invalid"].append(key)
        out[key] = value
    return out, report


def normalize_config(config: DeployConfig, *, check: bool) -> int:
    path = Path(config.config_path)
    document = json.loads(path.read_text())
    new, report = normalize(document)
    for name, keys in report.items():
        if keys:
            print(f"   {name}: {sorted(keys)}")
    if report["invalid"]:
        print(
            "   credential keys holding a value, not a reference (fix by hand):",
            sorted(report["invalid"]),
        )
        return 1
    if new == document:
        print("   config: flat scalars, nothing to change")
        return 0
    if check:
        return 1
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(new, handle, indent=2)
    os.replace(temporary, path)
    print(f"   config normalized: {len(document)} -> {len(new)} keys")
    return 0


# -- probe-sso ---------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 arbora-sso-probe",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Site": "none",
}


def probe_sso(
    config: DeployConfig,
    ports: Ports,
    opener: urllib.request.OpenerDirector | None = None,
) -> int:
    """What a browser does, end to end: redirect, login form, sign in, callback, session."""
    failed: list[str] = []

    def report(ok: bool, check: str, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {check} {detail}".rstrip(), flush=True)
        if not ok:
            failed.append(check)
        return ok

    jar = http.cookiejar.CookieJar()
    opener = opener or urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(
            context=ssl.create_default_context(cafile=config.ca_bundle)
        ),
        _NoRedirect(),
    )

    def get(url: str, data: bytes | None = None) -> tuple[int, Any, bytes]:
        headers = dict(BROWSER_HEADERS)
        if data is not None:
            headers["Sec-Fetch-Site"] = "same-origin"
        try:
            response = opener.open(
                urllib.request.Request(url, data=data, headers=headers), timeout=30
            )  # noqa: S310
            return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read() or b""

    def follow(
        url: str,
        data: bytes | None = None,
        stop: Callable[[str], bool] = lambda u: False,
    ) -> tuple[int, str, bytes]:
        for _ in range(12):
            status, headers, body = get(url, data)
            data = None
            location = (
                headers.get("Location") if status in (301, 302, 303, 307, 308) else None
            )
            if not location:
                return status, url, body
            url = urllib.parse.urljoin(url, location)
            if stop(url):
                return status, url, body
        return 0, url, b""

    is_auth = lambda u: "/protocol/openid-connect/auth" in u  # noqa: E731
    _, auth_url, _ = follow(config.public_url, stop=is_auth)
    report(is_auth(auth_url), "web UI redirects to the identity provider")
    if not is_auth(auth_url):
        return 1
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)
    report(
        auth_url.startswith(config.realm_url + "/"), "issuer is the configured realm"
    )
    report(
        (query.get("client_id") or [""])[0] == config.webui_client,
        f"client_id {config.webui_client}",
    )
    report(
        ((query.get("redirect_uri") or [""])[0]).endswith("/auth/callback"),
        "redirect_uri is the web UI callback",
    )
    status, _, body = follow(auth_url)
    page = body.decode(errors="replace")
    report(
        status == 200 and "Invalid parameter" not in page,
        "login page",
        f"http={status}",
    )
    match = re.search(
        r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page
    ) or re.search(r'<form[^>]+action="([^"]+login-actions/authenticate[^"]*)"', page)
    if not report(match is not None, "login form present") or match is None:
        return 1
    action = html.unescape(match.group(1))
    admin = ports.admin
    found = [
        u
        for u in admin(f"/users?username={PROBE_USER}&exact=true")
        if u["username"] == PROBE_USER
    ]
    if not found:
        admin(
            "/users",
            "POST",
            {
                "username": PROBE_USER,
                "enabled": True,
                "emailVerified": True,
                "email": f"{PROBE_USER}@{config.host}",
                "firstName": "arbora",
                "lastName": "sso-probe",
                "requiredActions": [],
            },
        )
        found = admin(f"/users?username={PROBE_USER}&exact=true")
    user_id = found[0]["id"]
    admin(f"/users/{user_id}", "PUT", {"enabled": True, "requiredActions": []})
    password = secrets.token_urlsafe(24)
    admin(
        f"/users/{user_id}/reset-password",
        "PUT",
        {"type": "password", "value": password, "temporary": False},
    )
    try:
        form = urllib.parse.urlencode(
            {"username": PROBE_USER, "password": password, "credentialId": ""}
        ).encode()
        is_callback = lambda u: "/auth/callback" in u and "code=" in u  # noqa: E731
        status, callback, _ = follow(action, data=form, stop=is_callback)
        if not report(
            is_callback(callback),
            "identity provider authenticates and returns a code",
            f"http={status}",
        ):
            return 1
        status, final, _ = follow(callback)
        domain = urllib.parse.urlsplit(config.public_url).hostname or ""
        cookies = [c.name for c in jar if c.domain.endswith(domain)]
        report(
            status == 200 and bool(cookies),
            "callback: code exchanged, session set",
            f"http={status} cookies={len(cookies)}",
        )
        base = f"{urllib.parse.urlsplit(final).scheme}://{urllib.parse.urlsplit(config.public_url).netloc}"
        status, _, body = get(base + "/auth/session")
        try:
            session = json.loads(body or b"{}")
        except ValueError:
            session = {}
        authed = bool(
            session.get(
                "authenticated",
                session.get("active", session.get("user") or session.get("sub")),
            )
        )
        report(
            status == 200 and authed, "/auth/session authenticated", f"http={status}"
        )
        status, _, body = get(base + "/")
        report(
            status == 200 and b"<html" in body.lower(),
            "dashboard loads signed in",
            f"http={status}",
        )
    finally:
        admin(f"/users/{user_id}", "PUT", {"enabled": False})
        print("     probe user disabled again")
    print(f"sso probe: {'FAIL ' + ','.join(failed) if failed else 'PASS'}")
    return 1 if failed else 0


# -- check-in-pod ------------------------------------------------------------


class PodChecks:
    """The checks that run inside the pod against the process on its loopback."""

    def __init__(
        self,
        config: Mapping[str, Any],
        http_call: Callable[..., tuple[int, dict[str, str], bytes]],
    ) -> None:
        self.config = config
        self.http = http_call
        self.failed: list[str] = []
        self.session: str | None = None
        self.next_id = 0

    def report(self, ok: bool, check: str, detail: str) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {check} {detail}", flush=True)
        if not ok:
            self.failed.append(check)
        return ok

    @staticmethod
    def error_text(out: object) -> str:
        if isinstance(out, dict):
            error = out.get("error")
            if isinstance(error, dict):
                return f"{error.get('code')}: {str(error.get('message', ''))[:160]}"
            if error:
                return str(error)[:160]
        return "none"

    def op(
        self, op_id: str, params: dict[str, Any], *, idempotent: bool = False
    ) -> tuple[int, Any]:
        extra = {"Idempotency-Key": str(uuid.uuid4())} if idempotent else None
        status, _, raw = self.http(f"/api/v1/ops/{op_id}", params, extra, 300)
        try:
            return status, json.loads(raw or b"{}")
        except ValueError:
            return status, {"error": {"code": "non-json"}}

    def rpc(
        self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False
    ) -> tuple[int, dict[str, Any]]:
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        if not notify:
            self.next_id += 1
            body["id"] = self.next_id
        extra = {"Accept": "application/json, text/event-stream"}
        if self.session:
            extra["mcp-session-id"] = self.session
        status, headers, raw = self.http("/mcp", body, extra, 300)
        lowered = {k.lower(): v for k, v in headers.items()}
        self.session = lowered.get("mcp-session-id", self.session)
        if notify or not raw:
            return status, {}
        if "text/event-stream" in lowered.get("content-type", ""):
            last: dict[str, Any] = {}
            for line in raw.decode(errors="replace").splitlines():
                if line.startswith("data:"):
                    try:
                        last = json.loads(line[5:].strip())
                    except ValueError:
                        pass
            return status, last
        try:
            return status, dict(json.loads(raw))
        except ValueError:
            return status, {"error": {"message": "unreadable MCP response"}}

    def verify(self) -> None:
        status, out = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "arbora-deploy-verify", "version": "1"},
            },
        )
        self.rpc("notifications/initialized", notify=True)
        if not self.report(
            status == 200 and "result" in out, "mcp.initialize", f"http={status}"
        ):
            return
        status, out = self.rpc("tools/list")
        names = [t["name"] for t in (out.get("result") or {}).get("tools", [])]
        self.report(
            status == 200 and bool(names),
            "mcp.tools/list",
            f"http={status} tools={len(names)}",
        )
        if self.report(
            "find" in names and "ask" in names, "mcp resident verbs find+ask", ""
        ):
            for verb, params in (
                ("find", {"intent": "graph tools", "params": {}}),
                ("ask", {"op": "eg.graph.NodeCount", "params": {}}),
            ):
                status, out = self.rpc(
                    "tools/call", {"name": verb, "arguments": params}
                )
                refused = (
                    status != 200
                    or "error" in out
                    or (out.get("result") or {}).get("isError")
                )
                self.report(
                    not refused,
                    f"mcp.{verb}",
                    f"http={status} error={self.error_text(out)}",
                )
        status, out = self.op("query.uql", {"text": "MATCH () |> LIMIT 1"})
        self.report(
            status == 200 and self.error_text(out) == "none",
            "http.query.uql",
            f"http={status} error={self.error_text(out)}",
        )
        for server, preferred in self.config.get("fleet_checks", []):
            self.fleet(server, tuple(preferred))

    def fleet(self, server: str, preferred: tuple[str, ...]) -> None:
        status, found = self.op(
            "fleet.catalog.search",
            {"servers": [server], "kinds": ["tool"], "limit": 50},
        )
        names: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("name"), str):
                    names.append(node["name"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(found.get("value", found) if isinstance(found, dict) else found)
        tools = list(dict.fromkeys(names))
        self.report(
            status == 200 and bool(tools),
            f"fleet.catalog.search[{server}]",
            f"http={status} tools={len(tools)}",
        )
        tool = next((t for p in preferred for t in tools if t == p), None) or next(
            (
                t
                for t in tools
                if any(
                    k in t.lower()
                    for k in ("list", "get", "status", "whoami", "search")
                )
            ),
            None,
        )
        if tool is None:
            self.report(
                False, f"fleet.call[{server}]", "no read-only tool in the catalog"
            )
            return
        arguments = {"query": "arbora"} if "search" in tool.lower() else {}
        status, out = self.op(
            "fleet.call",
            {"server": server, "tool": tool, "arguments": arguments},
            idempotent=True,
        )
        self.report(
            status == 200 and self.error_text(out) == "none",
            f"fleet.call[{server}]",
            f"http={status} tool={tool}",
        )

    def ingest(self) -> None:
        """Re-ingest connector content: ``ingest.sources.sync`` for every listed source."""
        status, listed = self.op("ingest.sources.list", {})
        code = (
            (listed.get("error") or {}).get("code")
            if isinstance(listed, dict)
            else None
        )
        if status == 503 and code == "UNAVAILABLE":
            print(
                "SKIP ingest.sources.list http=503 UNAVAILABLE: this release composes no ingest runner",
                flush=True,
            )
            return
        value = listed.get("value", {}) if isinstance(listed, dict) else {}
        sources = (
            value.get("sources", value.get("items", []))
            if isinstance(value, dict)
            else value
        )
        ids = [
            i
            for i in (
                s
                if isinstance(s, str)
                else s.get("source_id") or s.get("id") or s.get("name")
                for s in sources or []
            )
            if i
        ]
        self.report(
            status == 200, "ingest.sources.list", f"http={status} sources={len(ids)}"
        )
        for source in ids:
            status, out = self.op(
                "ingest.sources.sync",
                {"source_id": source, "mode": "full"},
                idempotent=True,
            )
            self.report(
                status in (200, 202),
                f"ingest.sources.sync[{source}]",
                f"http={status} error={self.error_text(out)}",
            )


def _loopback_http(
    config: Mapping[str, Any], token: str
) -> Callable[..., tuple[int, dict[str, str], bytes]]:
    def call(
        path: str,
        body: object,
        extra: dict[str, str] | None = None,
        timeout: float = 300,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "Host": config["host"],
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            **(extra or {}),
        }
        request = urllib.request.Request(
            "http://127.0.0.1:8000" + path,
            json.dumps(body).encode(),
            method="POST",
            headers=headers,
        )  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read() or b""

    return call


def check_in_pod(config: DeployConfig, ports: Ports, mode: str) -> int:
    """Run ``mode`` (``verify`` or ``ingest``) inside the pod through ``kubectl exec``."""
    return _in_pod(config, ports, ["check", mode, json.dumps(config.to_mapping())])


def _pod_check(mode: str, config_json: str) -> int:
    from arbora.core.settings import load_config
    from arbora.security.request_identity import client_credentials_token
    from arbora.security.settings import security_settings

    load_config()
    config = json.loads(config_json)
    token = client_credentials_token(
        security_settings(RuntimeConfig()).kg_identity_oauth2
    )
    checks = PodChecks(config, _loopback_http(config, token))
    {"verify": checks.verify, "ingest": checks.ingest}[mode]()
    print(
        f"inpod {mode}: {'FAIL ' + ','.join(checks.failed) if checks.failed else 'PASS'}",
        flush=True,
    )
    return 1 if checks.failed else 0


# -- command line ------------------------------------------------------------

SUBCOMMANDS = ("provision", "enrol", "normalize-config", "probe-sso", "check-in-pod")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="provision_command", required=True)
    for name in SUBCOMMANDS:
        command = sub.add_parser(name)
        command.add_argument(
            "--config", required=True, type=Path, help="the deploy.yml file"
        )
        if name == "provision":
            command.add_argument("--workdir", required=True, type=Path)
            command.add_argument(
                "--apply",
                action="store_true",
                help="map missing roles; without it the run only checks",
            )
        if name == "normalize-config":
            command.add_argument("--check", action="store_true")
        if name == "check-in-pod":
            command.add_argument("mode", choices=("verify", "ingest"))


def run(args: argparse.Namespace, ports: Ports | None = None) -> int:
    try:
        config = DeployConfig.load(args.config)
        command = args.provision_command
        if command == "normalize-config":
            return normalize_config(config, check=args.check)
        ports = ports or Ports.real(config)
        if command == "provision":
            return provision(config, ports, args.workdir, apply=args.apply)
        if command == "enrol":
            return enrol(config, ports)
        if command == "probe-sso":
            return probe_sso(config, ports)
        return check_in_pod(config, ports, args.mode)
    except ConfigError as exc:
        print(f"arbora deploy {args.provision_command}: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arbora deploy", description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    # In a pod the module is the stdin payload: ``python -I - enrol-apply <json>``.
    arguments = sys.argv[1:]
    if arguments[:1] == ["enrol-apply"]:
        raise SystemExit(_pod_enrol(arguments[1]))
    if arguments[:1] == ["check"]:
        raise SystemExit(_pod_check(arguments[1], arguments[2]))
    raise SystemExit(main(arguments))
