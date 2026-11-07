"""The declared identity file: the one schema of what the identity provider holds.

ARBORA-DEPLOY-R603. An environment supplies a declarative file (YAML or JSON)
listing realm settings, realm roles, groups, client scopes, the dynamic client
registration trusted hosts, clients (public, confidential and service-account),
and the human accounts. ``arbora identity reconcile --file`` reads it, plans the
difference against the provider, applies it idempotently and reports drift
(:mod:`arbora.identity.declaration_reconcile`). The environment carries no
reconcile code; this module is the schema and its validation.

The schema is closed: an unknown key is refused so a typo never becomes a silent
no-op. The file never holds a secret value, only references to them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbora.identity.client_contract import (
    NATIVE_DEFAULT_SCOPES,
    ClientSpec,
    ContractConfig,
    client_representation,
)

SCHEMA_VERSION = 1
CLIENT_KINDS = ("public", "confidential", "service")


class DeclarationError(ValueError):
    """The declared identity file is invalid; the message names the key."""


def _only(raw: Mapping[str, Any], allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise DeclarationError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeclarationError(f"{where}: must be a mapping")
    return value


def _text(
    raw: Mapping[str, Any], key: str, where: str, *, required: bool = True
) -> str:
    value = raw.get(key)
    if value is None or value == "":
        if required:
            raise DeclarationError(f"{where}: {key} is required")
        return ""
    if not isinstance(value, str):
        raise DeclarationError(f"{where}: {key} must be text")
    return value


def _texts(raw: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = raw.get(key) or ()
    if isinstance(value, str) or not all(isinstance(item, str) for item in value):
        raise DeclarationError(f"{where}: {key} must be a list of text")
    return tuple(value)


def _mappers(raw: Mapping[str, Any], where: str) -> tuple[Mapping[str, Any], ...]:
    out: list[Mapping[str, Any]] = []
    for item in raw.get("mappers") or ():
        mapper = _mapping(item, f"{where}.mappers")
        _only(mapper, ("name", "protocolMapper", "config"), f"{where}.mappers")
        _text(mapper, "name", f"{where}.mappers")
        _text(mapper, "protocolMapper", f"{where}.mappers")
        out.append({"protocol": "openid-connect", **mapper})
    return tuple(out)


@dataclass(frozen=True, slots=True)
class AdminDecl:
    url: str
    realm: str
    client_id: str
    secret_ref: str
    ca_bundle: str = ""
    allow_broad: bool = False


@dataclass(frozen=True, slots=True)
class SecretStoreDecl:
    url: str
    mount: str
    token_ref: str
    ca_bundle: str = ""


@dataclass(frozen=True, slots=True)
class RoleDecl:
    name: str
    description: str = ""
    composites: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeDecl:
    name: str
    audience: str = ""
    realm_default: bool = False
    attach_to_all_clients: bool = False
    mappers: tuple[Mapping[str, Any], ...] = ()
    scope_roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DcrDecl:
    trusted_hosts: tuple[str, ...]
    client_uris_check: bool = True
    host_sending_check: bool = False


@dataclass(frozen=True, slots=True)
class ServiceAccountDecl:
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    roles: tuple[str, ...] = ()
    forbidden_roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SecretTarget:
    """Where a confidential client's secret is published (``apps/<path>``)."""

    path: str
    id_key: str
    secret_key: str


@dataclass(frozen=True, slots=True)
class ClientDecl:
    client_id: str
    kind: str
    name: str
    description: str = ""
    root_url: str = ""
    base_url: str = ""
    direct_access: bool = False
    redirect_uris: tuple[str, ...] = ()
    web_origins: tuple[str, ...] = ()
    pkce: str = ""
    post_logout_redirect_uris: tuple[str, ...] = ()
    default_scopes: tuple[str, ...] = ()
    mappers: tuple[Mapping[str, Any], ...] = ()
    service_account: ServiceAccountDecl | None = None
    secret_target: SecretTarget | None = None


@dataclass(frozen=True, slots=True)
class UserDecl:
    username: str
    email: str
    first_name: str
    last_name: str
    groups: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Declaration:
    realm: str
    admin: AdminDecl
    secret_store: SecretStoreDecl | None
    settings: Mapping[str, Any]
    roles: tuple[RoleDecl, ...]
    groups: tuple[str, ...]
    scopes: tuple[ScopeDecl, ...]
    dcr: DcrDecl | None
    clients: tuple[ClientDecl, ...]
    users: tuple[UserDecl, ...]

    def contract_config(self) -> ContractConfig:
        """The admin connection, in the form the realm admin adapter takes."""
        return ContractConfig(
            admin_url=self.admin.url,
            realm=self.realm,
            admin_client_id=self.admin.client_id,
            admin_client_secret_ref=self.admin.secret_ref,
            admin_realm=self.admin.realm,
            ca_bundle=self.admin.ca_bundle,
        )


def load_declaration(path: str | Path) -> Declaration:
    """Read and validate a declared identity file (``.json``, ``.yaml``, ``.yml``)."""
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if target.suffix == ".json":
        raw = json.loads(text)
    else:
        import yaml

        raw = yaml.safe_load(text)
    return parse_declaration(raw)


def parse_declaration(raw: Any) -> Declaration:
    """Validate the parsed file against the closed schema."""
    top = _mapping(raw, "identity file")
    _only(
        top,
        (
            "version", "realm", "admin", "secret_store", "settings", "roles",
            "groups", "scopes", "dcr", "clients", "users",
        ),
        "identity file",
    )  # fmt: skip
    if top.get("version") != SCHEMA_VERSION:
        raise DeclarationError(f"version must be {SCHEMA_VERSION}")
    realm = _text(top, "realm", "identity file")
    declared = Declaration(
        realm=realm,
        admin=_admin(top),
        secret_store=_secret_store(top),
        settings=dict(_mapping(top.get("settings") or {}, "settings")),
        roles=tuple(_role(r) for r in top.get("roles") or ()),
        groups=_texts(top, "groups", "identity file"),
        scopes=tuple(_scope(s) for s in top.get("scopes") or ()),
        dcr=_dcr(top),
        clients=tuple(_client(c) for c in top.get("clients") or ()),
        users=tuple(_user(u) for u in top.get("users") or ()),
    )
    _cross_check(declared)
    return declared


def _admin(top: Mapping[str, Any]) -> AdminDecl:
    raw = _mapping(top.get("admin"), "admin")
    _only(
        raw,
        ("url", "realm", "client_id", "secret_ref", "ca_bundle", "allow_broad"),
        "admin",
    )
    return AdminDecl(
        url=_text(raw, "url", "admin").rstrip("/"),
        realm=_text(raw, "realm", "admin", required=False) or "master",
        client_id=_text(raw, "client_id", "admin"),
        secret_ref=_text(raw, "secret_ref", "admin"),
        ca_bundle=_text(raw, "ca_bundle", "admin", required=False),
        allow_broad=bool(raw.get("allow_broad", False)),
    )


def _secret_store(top: Mapping[str, Any]) -> SecretStoreDecl | None:
    if not top.get("secret_store"):
        return None
    raw = _mapping(top["secret_store"], "secret_store")
    _only(raw, ("url", "mount", "token_ref", "ca_bundle"), "secret_store")
    return SecretStoreDecl(
        url=_text(raw, "url", "secret_store").rstrip("/"),
        mount=_text(raw, "mount", "secret_store"),
        token_ref=_text(raw, "token_ref", "secret_store"),
        ca_bundle=_text(raw, "ca_bundle", "secret_store", required=False),
    )


def _role(item: Any) -> RoleDecl:
    raw = _mapping(item, "roles")
    _only(raw, ("name", "description", "composites"), "roles")
    return RoleDecl(
        _text(raw, "name", "roles"),
        _text(raw, "description", "roles", required=False),
        _texts(raw, "composites", "roles"),
    )


def _scope(item: Any) -> ScopeDecl:
    raw = _mapping(item, "scopes")
    _only(
        raw,
        ("name", "audience", "realm_default", "attach_to_all_clients", "mappers",
         "scope_roles"),
        "scopes",
    )  # fmt: skip
    name = _text(raw, "name", "scopes")
    return ScopeDecl(
        name=name,
        audience=_text(raw, "audience", f"scopes.{name}", required=False),
        realm_default=bool(raw.get("realm_default", False)),
        attach_to_all_clients=bool(raw.get("attach_to_all_clients", False)),
        mappers=_mappers(raw, f"scopes.{name}"),
        scope_roles=_texts(raw, "scope_roles", f"scopes.{name}"),
    )


def _dcr(top: Mapping[str, Any]) -> DcrDecl | None:
    if not top.get("dcr"):
        return None
    raw = _mapping(top["dcr"], "dcr")
    _only(raw, ("trusted_hosts", "client_uris_check", "host_sending_check"), "dcr")
    hosts = _texts(raw, "trusted_hosts", "dcr")
    if not hosts:
        raise DeclarationError("dcr: trusted_hosts must not be empty")
    return DcrDecl(
        hosts,
        bool(raw.get("client_uris_check", True)),
        bool(raw.get("host_sending_check", False)),
    )


def _client(item: Any) -> ClientDecl:
    raw = _mapping(item, "clients")
    _only(
        raw,
        ("clientId", "kind", "name", "description", "rootUrl", "baseUrl",
         "direct_access", "redirectUris",
         "webOrigins", "pkce", "postLogoutRedirectUris", "default_scopes",
         "mappers", "service_account", "secret_target"),
        "clients",
    )  # fmt: skip
    cid = _text(raw, "clientId", "clients")
    where = f"clients.{cid}"
    kind = _text(raw, "kind", where)
    if kind not in CLIENT_KINDS:
        raise DeclarationError(
            f"{where}: kind must be one of {', '.join(CLIENT_KINDS)}"
        )
    account = None
    if raw.get("service_account"):
        sa = _mapping(raw["service_account"], f"{where}.service_account")
        _only(
            sa,
            ("email", "first_name", "last_name", "roles", "forbidden_roles"),
            f"{where}.service_account",
        )
        account = ServiceAccountDecl(
            _text(sa, "email", where, required=False),
            _text(sa, "first_name", where, required=False),
            _text(sa, "last_name", where, required=False),
            _texts(sa, "roles", where),
            _texts(sa, "forbidden_roles", where),
        )
    if kind == "service" and account is None:
        account = ServiceAccountDecl()
    if kind != "service" and account is not None:
        raise DeclarationError(f"{where}: service_account needs kind service")
    target = None
    if raw.get("secret_target"):
        tr = _mapping(raw["secret_target"], f"{where}.secret_target")
        _only(tr, ("path", "id_key", "secret_key"), f"{where}.secret_target")
        if kind == "public":
            raise DeclarationError(f"{where}: a public client has no secret")
        target = SecretTarget(
            _text(tr, "path", where),
            _text(tr, "id_key", where),
            _text(tr, "secret_key", where),
        )
    logout = _texts(raw, "postLogoutRedirectUris", where)
    if kind == "public" and not _texts(raw, "redirectUris", where):
        raise DeclarationError(f"{where}: a public client needs redirectUris")
    return ClientDecl(
        client_id=cid,
        kind=kind,
        name=_text(raw, "name", where, required=False),
        description=_text(raw, "description", where, required=False),
        root_url=_text(raw, "rootUrl", where, required=False),
        base_url=_text(raw, "baseUrl", where, required=False),
        direct_access=bool(raw.get("direct_access", False)),
        redirect_uris=_texts(raw, "redirectUris", where),
        web_origins=_texts(raw, "webOrigins", where),
        pkce=_pkce(_text(raw, "pkce", where, required=False), kind),
        post_logout_redirect_uris=logout,
        default_scopes=_texts(raw, "default_scopes", where)
        or (NATIVE_DEFAULT_SCOPES if kind == "public" else ()),
        mappers=_mappers(raw, where),
        service_account=account,
        secret_target=target,
    )


def _pkce(value: str, kind: str) -> str:
    """``S256`` is the public default; ``none`` declares that PKCE is not required."""
    if value == "none":
        return ""
    return value or ("S256" if kind == "public" else "")


def _user(item: Any) -> UserDecl:
    raw = _mapping(item, "users")
    _only(
        raw,
        ("username", "email", "first_name", "last_name", "groups", "roles"),
        "users",
    )
    name = _text(raw, "username", "users")
    return UserDecl(
        username=name,
        email=_text(raw, "email", f"users.{name}"),
        first_name=_text(raw, "first_name", f"users.{name}", required=False),
        last_name=_text(raw, "last_name", f"users.{name}", required=False),
        groups=_texts(raw, "groups", f"users.{name}"),
        roles=_texts(raw, "roles", f"users.{name}"),
    )


def _cross_check(declared: Declaration) -> None:
    """Every reference resolves inside the file or is a provider built-in."""
    roles = {role.name for role in declared.roles}
    groups = set(declared.groups)
    for role in declared.roles:
        for member in role.composites:
            if member == role.name:
                raise DeclarationError(
                    f"roles.{role.name}: a role cannot contain itself"
                )
    for user in declared.users:
        for group in user.groups:
            if group not in groups:
                raise DeclarationError(
                    f"users.{user.username}: undeclared group {group}"
                )
        for role_name in user.roles:
            if role_name not in roles:
                raise DeclarationError(
                    f"users.{user.username}: undeclared role {role_name}"
                )
    seen: set[str] = set()
    for client in declared.clients:
        if client.client_id in seen:
            raise DeclarationError(f"clients.{client.client_id}: declared twice")
        seen.add(client.client_id)
        account = client.service_account
        if account is not None:
            overlap = set(account.roles) & set(account.forbidden_roles)
            if overlap:
                raise DeclarationError(
                    f"clients.{client.client_id}: role both granted and forbidden"
                )


def client_wanted(client: ClientDecl) -> dict[str, Any]:
    """The admin representation of a declared client (only the fields we own).

    A public client with no extra fields renders exactly as the native client of
    :mod:`arbora.identity.client_contract`, so a matching live client is never
    rewritten.
    """
    if client.kind == "public":
        spec = ClientSpec(
            client.client_id,
            client.name or client.client_id,
            client.redirect_uris,
            client.default_scopes,
        )
        wanted = client_representation(spec)
        wanted["attributes"]["pkce.code.challenge.method"] = client.pkce
        wanted["attributes"].pop("post.logout.redirect.uris")
        wanted["directAccessGrantsEnabled"] = client.direct_access
        wanted["webOrigins"] = list(client.web_origins)
    else:
        service = client.kind == "service"
        wanted = {
            "clientId": client.client_id,
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "clientAuthenticatorType": "client-secret",
            "standardFlowEnabled": not service,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": client.direct_access,
            "serviceAccountsEnabled": service,
            "redirectUris": list(client.redirect_uris),
            "webOrigins": list(client.web_origins),
            "attributes": {"pkce.code.challenge.method": client.pkce},
        }
    if client.post_logout_redirect_uris:
        wanted["attributes"]["post.logout.redirect.uris"] = "##".join(
            client.post_logout_redirect_uris
        )
    if client.kind != "public" and client.name:
        wanted["name"] = client.name
    if client.description:
        wanted["description"] = client.description
    if client.root_url:
        wanted["rootUrl"] = client.root_url
    if client.base_url:
        wanted["baseUrl"] = client.base_url
    return wanted
