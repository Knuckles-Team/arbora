"""Reconcile the identity provider against a declared identity file.

ARBORA-DEPLOY-R603. :func:`reconcile` plans the difference between the declared
file (:mod:`arbora.identity.declaration`) and the provider, applies it through
the realm admin interface, then re-plans to prove the result converged. The pass
is idempotent: a second run, or a run against a provider that already matches,
writes nothing. It only creates, updates and adds; it never deletes a client,
role, group, scope or user, and it never grants a role the file forbids.

``plan`` and ``check`` are the same walk with writes switched off: ``plan``
prints what would change, ``check`` exits non-zero when anything would (drift).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from arbora.identity.client_contract import (
    AdminFactory,
    ContractFailure,
    RealmAdmin,
    audience_mapper,
    default_admin_factory,
    scope_representation,
)
from arbora.identity.client_contract import ScopeSpec as _AudienceScope
from arbora.identity.declaration import (
    ClientDecl,
    Declaration,
    DeclarationError,
    ScopeDecl,
    SecretTarget,
    client_wanted,
    load_declaration,
)

logger = logging.getLogger(__name__)

DCR_POLICY_TYPE = (
    "org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
)

#: ``(target, values) -> written``: publish a client's secret; ``False`` when equal.
SecretSink = Callable[[SecretTarget, Mapping[str, str]], bool]
SinkFactory = Callable[[Declaration], "SecretSink | None"]


@dataclass(slots=True)
class Plan:
    """What a walk found: ``changes`` to make and ``violations`` to refuse."""

    changes: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def converged(self) -> bool:
        return not self.changes and not self.violations


def _q(value: str) -> str:
    return quote(value, safe="")


def _differs(found: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    """True when a declared field differs; an empty text equals a missing one."""
    for key, value in wanted.items():
        if key == "attributes":
            have = found.get("attributes") or {}
            if any((have.get(k) or "") != (v or "") for k, v in value.items()):
                return True
        elif key in ("redirectUris", "webOrigins"):
            if sorted(found.get(key) or []) != sorted(value):
                return True
        elif (found.get(key) if found.get(key) is not None else "") != (
            value if value is not None else ""
        ):
            return True
    return False


class _Walk:
    """One walk over the declaration; writes only when ``apply`` is set."""

    def __init__(
        self,
        decl: Declaration,
        admin: RealmAdmin,
        sink: SecretSink | None,
        *,
        apply: bool,
    ) -> None:
        self.decl = decl
        self.admin = admin
        self.sink = sink
        self.apply = apply
        self.base = f"/admin/realms/{decl.realm}"
        self.plan = Plan()

    # -- helpers ----------------------------------------------------------- #
    def get(self, rel: str) -> Any:
        return self.admin.call("GET", self.base + rel)

    def write(self, label: str, method: str, rel: str, body: Any = None) -> None:
        self.plan.changes.append(label)
        if self.apply:
            self.admin.call(method, self.base + rel, body)

    def refuse(self, item: str) -> None:
        self.plan.violations.append(item)

    # -- the walk ---------------------------------------------------------- #
    def run(self) -> Plan:
        self.settings()
        self.roles()
        self.groups()
        scope_ids = self.scopes()
        self.dcr()
        client_ids = self.clients(scope_ids)
        self.attach_everywhere(scope_ids)
        self.users()
        del client_ids
        return self.plan

    def settings(self) -> None:
        if not self.decl.settings:
            return
        found = self.admin.call("GET", self.base) or {}
        diff = {
            key: value
            for key, value in self.decl.settings.items()
            if found.get(key) != value
        }
        if diff:
            self.plan.changes.append(f"realm:{','.join(sorted(diff))}")
            if self.apply:
                self.admin.call("PUT", self.base, diff)

    def roles(self) -> None:
        have = {r["name"]: r for r in self.get("/roles?max=1000") or ()}
        declared = {role.name for role in self.decl.roles}
        for role in self.decl.roles:
            found = have.get(role.name)
            if found is None:
                body = {"name": role.name, "description": role.description}
                self.write(f"role:{role.name}", "POST", "/roles", body)
            elif role.description and found.get("description") != role.description:
                body = {**found, "description": role.description}
                self.write(f"role:{role.name}", "PUT", f"/roles/{_q(role.name)}", body)
        for role in self.decl.roles:
            if not role.composites:
                continue
            exists = role.name in have
            members = (
                {
                    m["name"]
                    for m in self.get(f"/roles/{_q(role.name)}/composites") or ()
                }
                if exists
                else set()
            )
            missing = [name for name in role.composites if name not in members]
            if not missing:
                continue
            reps = []
            for name in missing:
                if name in have or name not in declared or self.apply:
                    reps.append(self.get(f"/roles/{_q(name)}"))
            self.plan.changes.append(f"composite:{role.name}:{','.join(missing)}")
            if self.apply:
                self.admin.call(
                    "POST", f"{self.base}/roles/{_q(role.name)}/composites", reps
                )

    def groups(self) -> None:
        have = {g["name"] for g in self.get("/groups?max=1000") or ()}
        for name in self.decl.groups:
            if name not in have:
                self.write(f"group:{name}", "POST", "/groups", {"name": name})

    def scopes(self) -> dict[str, str]:
        def ids() -> dict[str, str]:
            return {
                str(s["name"]): str(s["id"]) for s in self.get("/client-scopes") or ()
            }

        have = ids()
        for spec in self.decl.scopes:
            audience = _AudienceScope(spec.name, spec.audience or None)
            if spec.name not in have:
                self.write(
                    f"scope:{spec.name}",
                    "POST",
                    "/client-scopes",
                    scope_representation(audience),
                )
                if self.apply:
                    have = ids()
            sid = have.get(spec.name)
            self.scope_mappers(spec, audience, sid)
            self.scope_defaults(spec, sid)
        return have

    def scope_mappers(
        self, spec: ScopeDecl, audience: _AudienceScope, sid: str | None
    ) -> None:
        rel = f"/client-scopes/{sid}/protocol-mappers/models"
        have = list(self.get(rel) or ()) if sid else []
        if spec.audience and not any(
            (m.get("config") or {}).get("included.custom.audience") == spec.audience
            for m in have
        ):
            self.write(
                f"mapper:{spec.name}:audience", "POST", rel, audience_mapper(audience)
            )
        names = {m["name"] for m in have}
        for mapper in spec.mappers:
            if mapper["name"] not in names:
                self.write(
                    f"mapper:{spec.name}:{mapper['name']}", "POST", rel, dict(mapper)
                )

    def scope_defaults(self, spec: ScopeDecl, sid: str | None) -> None:
        if spec.realm_default:
            current = {
                s["name"] for s in self.get("/default-default-client-scopes") or ()
            }
            if spec.name not in current:
                self.write(
                    f"realm-default-scope:{spec.name}",
                    "PUT",
                    f"/default-default-client-scopes/{sid}",
                )
        if spec.scope_roles and sid:
            rel = f"/client-scopes/{sid}/scope-mappings/realm"
            held = {r["name"] for r in self.get(rel) or ()}
            missing = [n for n in spec.scope_roles if n not in held]
            if missing:
                reps = [self.get(f"/roles/{_q(n)}") for n in missing]
                self.write(
                    f"scope-roles:{spec.name}:{','.join(missing)}", "POST", rel, reps
                )

    def dcr(self) -> None:
        wanted = self.decl.dcr
        if wanted is None:
            return
        comps = self.get(f"/components?type={DCR_POLICY_TYPE}") or ()
        trusted = [
            c
            for c in comps
            if c.get("providerId") == "trusted-hosts"
            and c.get("subType") == "anonymous"
        ]
        if len(trusted) != 1:
            self.refuse("dcr:trusted-hosts-component")
            return
        comp = trusted[0]
        config = {
            "trusted-hosts": list(wanted.trusted_hosts),
            "client-uris-must-match": [str(wanted.client_uris_check).lower()],
            "host-sending-registration-request-must-match": [
                str(wanted.host_sending_check).lower()
            ],
        }
        have = comp.get("config") or {}
        same = sorted(have.get("trusted-hosts") or []) == sorted(
            config["trusted-hosts"]
        )
        same = same and all(
            have.get(k) == v for k, v in config.items() if k != "trusted-hosts"
        )
        if not same:
            body = {**comp, "config": {**have, **config}}
            self.write("dcr:trusted-hosts", "PUT", f"/components/{comp['id']}", body)

    # -- clients ----------------------------------------------------------- #
    def find_client(self, client_id: str) -> dict[str, Any] | None:
        found = self.get(f"/clients?clientId={_q(client_id)}") or []
        return dict(found[0]) if found else None

    def clients(self, scope_ids: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for client in self.decl.clients:
            uuid = self.client(client, scope_ids)
            if uuid:
                out[client.client_id] = uuid
        return out

    def client(self, client: ClientDecl, scope_ids: Mapping[str, str]) -> str | None:
        wanted = client_wanted(client)
        found = self.find_client(client.client_id)
        label = f"client:{client.client_id}"
        if found is None:
            self.plan.changes.append(label)
            if not self.apply:
                return None
            self.admin.call("POST", f"{self.base}/clients", wanted)
            found = self.find_client(client.client_id)
            if found is None:
                raise ContractFailure("client_missing", (label,))
        elif _differs(found, wanted):
            merged = {**found, **wanted}
            merged["attributes"] = {
                **(found.get("attributes") or {}),
                **wanted["attributes"],
            }
            self.write(label, "PUT", f"/clients/{found['id']}", merged)
        uuid = str(found["id"])
        self.client_scopes(client, uuid, scope_ids)
        self.client_mappers(client, uuid)
        if client.service_account is not None:
            self.service_account(client, uuid)
        self.publish_secret(client, uuid)
        return uuid

    def client_scopes(
        self, client: ClientDecl, uuid: str, scope_ids: Mapping[str, str]
    ) -> None:
        rel = f"/clients/{uuid}/default-client-scopes"
        present = {str(s.get("name")) for s in self.get(rel) or ()}
        for name in client.default_scopes:
            if name in present:
                continue
            if name not in scope_ids:
                self.refuse(f"scope:{name}")
                continue
            self.write(
                f"default-scope:{client.client_id}:{name}",
                "PUT",
                f"{rel}/{scope_ids[name]}",
            )

    def client_mappers(self, client: ClientDecl, uuid: str) -> None:
        if not client.mappers:
            return
        rel = f"/clients/{uuid}/protocol-mappers/models"
        have = {m["name"]: m for m in self.get(rel) or ()}
        for mapper in client.mappers:
            label = f"mapper:{client.client_id}:{mapper['name']}"
            found = have.get(mapper["name"])
            if found is None:
                self.write(label, "POST", rel, dict(mapper))
            elif _differs(
                {"config": found.get("config")}, {"config": mapper.get("config") or {}}
            ) and any(
                (found.get("config") or {}).get(k) != v
                for k, v in (mapper.get("config") or {}).items()
            ):
                body = {**found, **mapper, "id": found["id"]}
                self.write(label, "PUT", f"{rel}/{found['id']}", body)

    def service_account(self, client: ClientDecl, uuid: str) -> None:
        account = client.service_account
        if account is None:
            return
        user = self.get(f"/clients/{uuid}/service-account-user") or {}
        uid = user.get("id")
        profile = {
            key: value
            for key, value in (
                ("email", account.email),
                ("firstName", account.first_name),
                ("lastName", account.last_name),
            )
            if value
        }
        if profile and any(user.get(k) != v for k, v in profile.items()):
            body = {
                **profile,
                "emailVerified": bool(account.email) or user.get("emailVerified"),
            }
            self.write(
                f"service-account:{client.client_id}:profile",
                "PUT",
                f"/users/{uid}",
                body,
            )
        held = {r["name"] for r in self.get(f"/users/{uid}/role-mappings/realm") or ()}
        for name in account.forbidden_roles:
            if name in held:
                self.refuse(f"forbidden-role:{client.client_id}:{name}")
        missing = [n for n in account.roles if n not in held]
        if missing:
            reps = [self.get(f"/roles/{_q(n)}") for n in missing]
            self.write(
                f"service-account:{client.client_id}:roles:{','.join(missing)}",
                "POST",
                f"/users/{uid}/role-mappings/realm",
                reps,
            )

    def publish_secret(self, client: ClientDecl, uuid: str) -> None:
        target = client.secret_target
        if target is None or self.sink is None or not self.apply:
            return
        value = (self.get(f"/clients/{uuid}/client-secret") or {}).get("value")
        if not value:
            self.refuse(f"secret:{client.client_id}")
            return
        values = {target.id_key: client.client_id, target.secret_key: str(value)}
        if self.sink(target, values):
            self.plan.changes.append(f"secret:{client.client_id}:{target.path}")

    def attach_everywhere(self, scope_ids: Mapping[str, str]) -> None:
        wanted = [s for s in self.decl.scopes if s.attach_to_all_clients]
        if not wanted:
            return
        for client in self.get("/clients?max=1000") or ():
            if client.get(
                "protocol", "openid-connect"
            ) != "openid-connect" or client.get("bearerOnly"):
                continue
            rel = f"/clients/{client['id']}/default-client-scopes"
            attached = {s["name"] for s in self.get(rel) or ()}
            for spec in wanted:
                sid = scope_ids.get(spec.name)
                if spec.name not in attached and sid:
                    self.write(
                        f"default-scope:{client['clientId']}:{spec.name}",
                        "PUT",
                        f"{rel}/{sid}",
                    )

    # -- users ------------------------------------------------------------- #
    def users(self) -> None:
        groups = {g["name"]: g["id"] for g in self.get("/groups?max=1000") or ()}
        for user in self.decl.users:
            found = [
                u
                for u in self.get(f"/users?exact=true&username={_q(user.username)}")
                or ()
                if u["username"] == user.username
            ]
            if not found:
                if self.get(f"/users?exact=true&email={_q(user.email)}"):
                    self.refuse(
                        f"user:{user.username}:email-belongs-to-another-account"
                    )
                    continue
                body = {
                    "username": user.username,
                    "email": user.email,
                    "firstName": user.first_name,
                    "lastName": user.last_name,
                    "enabled": True,
                    "requiredActions": ["UPDATE_PASSWORD", "VERIFY_EMAIL"],
                }
                self.write(f"user:{user.username}", "POST", "/users", body)
                if not self.apply:
                    continue
                found = self.get(f"/users?exact=true&username={_q(user.username)}")
            self.user_membership(
                user.username, found[0], user.groups, user.roles, groups
            )

    def user_membership(
        self,
        name: str,
        found: Mapping[str, Any],
        member_of: tuple[str, ...],
        roles: tuple[str, ...],
        groups: Mapping[str, str],
    ) -> None:
        uid = found["id"]
        decl = next(u for u in self.decl.users if u.username == name)
        names = {"firstName": decl.first_name, "lastName": decl.last_name}
        names = {k: v for k, v in names.items() if v}
        if any(found.get(k) != v for k, v in names.items()):
            self.write(f"user:{name}:name", "PUT", f"/users/{uid}", names)
        current = {g["name"] for g in self.get(f"/users/{uid}/groups") or ()}
        for group in member_of:
            if group not in current and group in groups:
                self.write(
                    f"user:{name}:join:{group}",
                    "PUT",
                    f"/users/{uid}/groups/{groups[group]}",
                )
        held = {r["name"] for r in self.get(f"/users/{uid}/role-mappings/realm") or ()}
        missing = [r for r in roles if r not in held]
        if missing:
            reps = [self.get(f"/roles/{_q(r)}") for r in missing]
            self.write(
                f"user:{name}:roles:{','.join(missing)}",
                "POST",
                f"/users/{uid}/role-mappings/realm",
                reps,
            )


@dataclass(frozen=True, slots=True)
class DeclaredOutcome:
    """The result of a declared reconcile."""

    state: str
    changed: tuple[str, ...]
    violations: tuple[str, ...]
    code: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.state in ("converged", "planned") else 1


def reconcile(
    decl: Declaration,
    admin: RealmAdmin,
    sink: SecretSink | None = None,
    *,
    mode: str = "apply",
) -> DeclaredOutcome:
    """Run ``apply``, ``plan`` or ``check`` over the provider behind ``admin``."""
    if mode not in ("apply", "plan", "check"):
        raise ValueError(f"unknown mode {mode!r}")
    try:
        first = _Walk(decl, admin, sink, apply=mode == "apply").run()
        if mode == "plan":
            return DeclaredOutcome(
                "planned", tuple(first.changes), tuple(first.violations)
            )
        if mode == "check":
            state = "converged" if first.converged else "drift"
            return DeclaredOutcome(state, tuple(first.changes), tuple(first.violations))
        verify = _Walk(decl, admin, None, apply=False).run()
    except ContractFailure as failure:
        return DeclaredOutcome("degraded", (), failure.items, failure.code)
    state = "converged" if verify.converged else "degraded"
    code = None if verify.converged else "client_drift"
    return DeclaredOutcome(
        state, tuple(first.changes), tuple(first.violations + verify.violations), code
    )


def openbao_sink_factory(decl: Declaration) -> SecretSink | None:
    """A KV v2 sink over the declared secret store, or ``None`` when none is declared."""
    store = decl.secret_store
    if store is None:
        return None
    import httpx
    from agent_connector_sdk.credentials.runtime_references import (
        resolve_runtime_secret_reference,
    )

    token = resolve_runtime_secret_reference(store.token_ref)
    client = httpx.Client(
        base_url=store.url,
        verify=store.ca_bundle or True,
        timeout=20.0,
        headers={"X-Vault-Token": token},
    )

    def publish(target: SecretTarget, values: Mapping[str, str]) -> bool:
        url = f"/v1/{store.mount}/data/{target.path}"
        current = client.get(url)
        if current.status_code == 200:
            held = (current.json().get("data") or {}).get("data") or {}
            if all(held.get(k) == v for k, v in values.items()):
                return False
            reply = client.request(
                "PATCH",
                url,
                content=json.dumps({"data": dict(values)}),
                headers={"Content-Type": "application/merge-patch+json"},
            )
        elif current.status_code == 404:
            reply = client.post(url, json={"data": dict(values)})
        else:
            raise ContractFailure("admin_refused", (f"secret:{target.path}",))
        if reply.status_code >= 400:
            raise ContractFailure("admin_refused", (f"secret:{target.path}",))
        return True

    return publish


def reconcile_file(
    path: str,
    *,
    mode: str = "apply",
    admin_factory: AdminFactory | None = None,
    sink_factory: SinkFactory = openbao_sink_factory,
) -> DeclaredOutcome:
    """Load the file, open the admin connection and reconcile."""
    decl = load_declaration(path)
    factory = admin_factory or _declared_admin_factory(decl)
    try:
        admin = factory(decl.contract_config())
        sink = sink_factory(decl) if mode == "apply" else None
    except ContractFailure as failure:
        return DeclaredOutcome("degraded", (), failure.items, failure.code)
    return reconcile(decl, admin, sink, mode=mode)


def _declared_admin_factory(decl: Declaration) -> AdminFactory:
    if not decl.admin.allow_broad:
        return default_admin_factory

    def broad(config: Any) -> RealmAdmin:
        from agent_connector_sdk.credentials.runtime_references import (
            resolve_runtime_secret_reference,
        )

        from arbora.identity.client_contract import http_transport

        try:
            secret = resolve_runtime_secret_reference(config.admin_client_secret_ref)
        except Exception as exc:
            raise ContractFailure("admin_credential_missing") from exc
        return RealmAdmin(config, http_transport(config, secret, allow_broad=True))

    return broad


def declared_command(argv: list[str]) -> int:
    """``arbora identity reconcile --file F [--plan|--check]``; prints one JSON line."""
    path = argv[argv.index("--file") + 1] if "--file" in argv else ""
    mode = "plan" if "--plan" in argv else "check" if "--check" in argv else "apply"
    try:
        outcome = reconcile_file(path, mode=mode)
    except (DeclarationError, OSError, ValueError) as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "state": outcome.state,
                "code": outcome.code,
                "changed": list(outcome.changed),
                "violations": list(outcome.violations),
            }
        )
    )
    return outcome.exit_code
