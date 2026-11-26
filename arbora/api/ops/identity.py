"""Identity administration operations (ARBORA-IDENTITY-R018).

Every operation sends one engine ``Identity`` request through the
``IdentityAdminPort`` that the composition root binds as ``identity_admin``
(``arbora/identity/composition.py``). Arbora holds no user, role, group,
provider, session or audit record: each result is what the engine returned.
Administrator operations need the exact scope ``identity:admin``; the invoke
pipeline refuses them off the console or after a stale second-factor check
(``arbora.api.invoke.pipeline.identity_gate``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    OpSpec,
    PrincipalRule,
    Surface,
    Verb,
)
from arbora.identity.engine_admin import IdentityRefused

ADMIN_SCOPE = "identity:admin"
SELF_SCOPE = "identity:self"
_PAGE = 100
_NOT_FOUND = {"IDENTITY_NOT_FOUND": "USER_NOT_FOUND"}


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(_P):
    pass


class Value(_P):
    value: dict[str, Any]


class UserRef(_P):
    user_id: str = Field(min_length=1, max_length=256)


class PageParams(_P):
    cursor: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=_PAGE, ge=1, le=500)


class UsersList(PageParams):
    query: str | None = Field(default=None, min_length=1, max_length=256)


class UserCreate(_P):
    username: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    roles: list[str] = Field(default_factory=list, max_length=100)
    groups: list[str] = Field(default_factory=list, max_length=100)


class ServiceAccountCreate(_P):
    username: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=256)
    roles: list[str] = Field(default_factory=list, max_length=100)


class ServiceAccountRef(_P):
    service_account_id: str = Field(min_length=1, max_length=256)


class ApiKeyRevoke(_P):
    key_id: str = Field(min_length=1, max_length=256)


class RoleChange(_P):
    user_id: str = Field(min_length=1, max_length=256)
    role_id: str = Field(min_length=1, max_length=256)


class RoleUpsert(_P):
    role: str = Field(min_length=1, max_length=256)
    scopes: list[str] = Field(default_factory=list, max_length=200)
    description: str | None = Field(default=None, max_length=1024)


class GroupUpsert(_P):
    group_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    roles: list[str] = Field(default_factory=list, max_length=100)
    mfa_required: bool | None = None


class GroupMembers(_P):
    group_id: str = Field(min_length=1, max_length=256)
    add: list[str] = Field(default_factory=list, max_length=500)
    remove: list[str] = Field(default_factory=list, max_length=500)


class MappingRule(_P):
    rule_id: str = Field(min_length=1, max_length=128)
    claim_path: str = Field(min_length=1, max_length=256)
    match_kind: Literal["equals", "prefix", "regex"]
    value: str = Field(max_length=256)
    target: str = Field(pattern=r"^(role|group):.+$", max_length=256)
    privileged: bool | None = None


class ProviderUpsert(_P):
    provider_id: str = Field(min_length=1, max_length=128)
    kind: Literal["oidc", "saml", "ldap", "scim"]
    display_name: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    config_json: str = Field(default="{}", max_length=16384)
    secret_ref: str | None = Field(default=None, pattern=r"^(env|openbao)://\S+$")
    email_domains: list[str] = Field(default_factory=list, max_length=100)
    jit_policy: Literal["deny", "create", "link_by_verified_email"] = "deny"
    order: int | None = Field(default=None, ge=0)
    rules: list[MappingRule] = Field(default_factory=list, max_length=200)


class ProviderRef(_P):
    provider_id: str = Field(min_length=1, max_length=128)


class MappingDryRun(ProviderRef):
    claims: dict[str, list[str]] = Field(max_length=100)


class PolicySet(_P):
    expected_epoch: int = Field(ge=0)
    password_min_chars: int | None = Field(default=None, ge=1, le=256)
    registration_policy: str | None = Field(default=None, max_length=64)
    local_fallback: str | None = Field(default=None, max_length=64)


class ModeTransition(_P):
    to: Literal["local", "external"]
    expected_epoch: int = Field(ge=0)
    issuer_kid: str = Field(min_length=1, max_length=256)
    ack: str | None = Field(default=None, max_length=256)
    local_fallback: str | None = Field(default=None, max_length=64)


class AuditExport(_P):
    from_ms: int = Field(ge=0, alias="from")
    to_ms: int = Field(ge=0, alias="to")
    cursor: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=_PAGE, ge=1, le=500)


async def _send(
    context: Any,
    family: str,
    op: str,
    request: Mapping[str, Any] | None = None,
    *,
    expect: str,
    refusals: Mapping[str, str] | None = None,
) -> Any:
    port = context.services.get("identity_admin")
    if port is None:
        raise RuntimeError("identity admin port is not bound")
    try:
        return await port.identity(
            context.client,
            context.caller.engine_claims,
            family,
            op,
            request,
            expect=expect,
        )
    except IdentityRefused as refused:
        code = (refusals or {}).get(refused.code)
        if code is not None:
            raise OperationRefused(code) from None
        raise OperationRefused(
            "INVALID_ARGUMENT", {"engine_code": refused.code}
        ) from None


def _page(rows: list[Any], limit: int, key: str) -> dict[str, Any]:
    full = len(rows) >= limit and bool(rows)
    return {"next_cursor": str(rows[-1][key]) if full else None}


async def handle_self_get(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    user = await _send(
        context,
        "user",
        "get",
        {"id": context.caller.principal},
        expect="user",
        refusals=_NOT_FOUND,
    )
    return {"value": {"user": user}}


async def handle_self_sessions_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    rows = await _send(
        context, "session", "list", {"id": context.caller.principal}, expect="sessions"
    )
    return {"value": {"sessions": rows}}


async def handle_users_list(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    request = {"after": params.get("cursor"), "limit": params["limit"]}
    rows = await _send(context, "user", "list", request, expect="users")
    page = _page(rows, params["limit"], "principal_id")
    query = (params.get("query") or "").casefold()
    if query:
        rows = [
            row
            for row in rows
            if any(
                query in str(row.get(field) or "").casefold()
                for field in ("username", "email", "display_name")
            )
        ]
    return {"value": {"users": rows, **page}}


async def handle_users_get(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    user = await _send(
        context,
        "user",
        "get",
        {"id": params["user_id"]},
        expect="user",
        refusals=_NOT_FOUND,
    )
    return {"value": {"user": user}}


async def _create(context: Any, request: dict[str, Any], kind: str) -> Any:
    principal = await _send(
        context,
        "user",
        "create",
        {**request, "kind": kind},
        expect="principal",
        refusals={"IDENTITY_COLLISION": "USER_EXISTS"},
    )
    return {"value": principal}


async def handle_users_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    request = {k: v for k, v in params.items() if v not in (None, [])}
    return await _create(context, request, "human")


async def handle_service_accounts_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    request = {k: v for k, v in params.items() if v not in (None, [])}
    return await _create(context, request, "service")


async def _set_status(context: Any, principal_id: str, status: str) -> Any:
    done = await _send(
        context,
        "user",
        "set_status",
        {"principal_id": principal_id, "status": status},
        expect="done",
        refusals={
            "IDENTITY_NOT_FOUND": "USER_NOT_FOUND",
            "IDENTITY_PRECONDITION_FAILED": "LAST_ADMIN_PROTECTED",
        },
    )
    return {"value": {"user_id": principal_id, "status": status, **done}}


async def handle_users_disable(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    return await _set_status(context, params["user_id"], "disabled")


async def handle_users_enable(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    return await _set_status(context, params["user_id"], "active")


async def handle_users_deprovision(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    await _set_status(context, params["user_id"], "deprovisioned")
    return {"value": {"deprovisioned": True}}


async def handle_service_accounts_disable(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    return await _set_status(context, params["service_account_id"], "disabled")


async def handle_service_accounts_deprovision(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    await _set_status(context, params["service_account_id"], "deprovisioned")
    return {"value": {"deprovisioned": True}}


async def handle_service_accounts_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    request = {"after": params.get("cursor"), "limit": params["limit"]}
    rows = await _send(context, "user", "list", request, expect="users")
    page = _page(rows, params["limit"], "principal_id")
    accounts = [row for row in rows if row.get("kind") == "service"]
    return {"value": {"service_accounts": accounts, **page}}


async def handle_users_unlock(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    await _send(
        context,
        "user",
        "unlock",
        {"id": params["user_id"]},
        expect="done",
        refusals=_NOT_FOUND,
    )
    return {"value": {"user_id": params["user_id"], "locked": False}}


async def handle_users_force_logout(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    sessions = await _send(
        context,
        "session",
        "list",
        {"id": params["user_id"]},
        expect="sessions",
        refusals=_NOT_FOUND,
    )
    live = sum(1 for row in sessions if not row.get("revoked"))
    await _send(
        context, "session", "revoke_all", {"id": params["user_id"]}, expect="done"
    )
    return {"value": {"revoked": live}}


async def handle_sessions_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    rows = await _send(
        context,
        "session",
        "list",
        {"id": params["user_id"]},
        expect="sessions",
        refusals=_NOT_FOUND,
    )
    return {"value": {"sessions": rows}}


async def handle_api_keys_revoke(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    await _send(
        context,
        "token",
        "revoke_api_key",
        {"id": params["key_id"]},
        expect="done",
        refusals={"IDENTITY_NOT_FOUND": "NOT_FOUND"},
    )
    return {"value": {"revoked": True}}


async def handle_roles_list(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    return {
        "value": {"roles": await _send(context, "access", "list_roles", expect="roles")}
    }


async def _role_change(context: Any, params: Mapping[str, Any], change: str) -> Any:
    done = await _send(
        context,
        "access",
        "change_user_role",
        {
            "principal_id": params["user_id"],
            "role_id": params["role_id"],
            "change": change,
        },
        expect="done",
        refusals={"IDENTITY_NOT_FOUND": "ROLE_UNKNOWN"},
    )
    return {
        "value": {"user_id": params["user_id"], "role_id": params["role_id"], **done}
    }


async def handle_roles_assign(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    return await _role_change(context, params, "add")


async def handle_roles_remove(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    return await _role_change(context, params, "remove")


async def handle_roles_upsert(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    request = {
        "role_id": params["role"],
        "name": params["role"],
        "scopes": params["scopes"],
        "description": params.get("description"),
    }
    await _send(
        context,
        "access",
        "upsert_role",
        request,
        expect="done",
        refusals={"IDENTITY_UNKNOWN_SCOPE": "SCOPE_UNKNOWN"},
    )
    roles = await _send(context, "access", "list_roles", expect="roles")
    role = next((r for r in roles if r.get("role_id") == params["role"]), None)
    return {"value": {"role": role}}


async def handle_groups_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    return {
        "value": {
            "groups": await _send(context, "access", "list_groups", expect="groups")
        }
    }


async def handle_groups_upsert(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    await _send(
        context,
        "access",
        "upsert_group",
        {k: v for k, v in params.items() if v is not None},
        expect="done",
        refusals={"IDENTITY_INVALID": "GROUP_INVALID"},
    )
    groups = await _send(context, "access", "list_groups", expect="groups")
    group = next((g for g in groups if g.get("group_id") == params["group_id"]), None)
    return {"value": {"group": group}}


async def handle_groups_set_members(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    refusals = {
        "IDENTITY_NOT_FOUND": "USER_NOT_FOUND",
        "IDENTITY_PRECONDITION_FAILED": "LAST_ADMIN_PROTECTED",
    }
    changed = 0
    for change, members in (("add", params["add"]), ("remove", params["remove"])):
        for member in members:
            done = await _send(
                context,
                "access",
                "change_membership",
                {
                    "group_id": params["group_id"],
                    "principal_id": member,
                    "change": change,
                },
                expect="done",
                refusals=refusals,
            )
            changed += 1 if done.get("changed") else 0
    return {"value": {"group_id": params["group_id"], "changed": changed}}


async def handle_providers_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    return {"value": {"providers": await _send(context, "idp", "list", expect="idps")}}


async def handle_providers_upsert(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    request = {
        "idp_id": params["provider_id"],
        "kind": params["kind"],
        "display_name": params["display_name"],
        "enabled": params["enabled"],
        "config_json": params["config_json"],
        "secret_ref": params.get("secret_ref"),
        "email_domains": params["email_domains"],
        "jit_policy": params["jit_policy"],
        "order": params.get("order"),
        "rules": params["rules"],
    }
    await _send(
        context,
        "idp",
        "upsert",
        request,
        expect="done",
        refusals={"IDENTITY_INVALID": "PROVIDER_INVALID"},
    )
    return {"value": {"provider_id": params["provider_id"]}}


async def handle_providers_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    await _send(
        context,
        "idp",
        "remove",
        {"id": params["provider_id"]},
        expect="done",
        refusals={"IDENTITY_NOT_FOUND": "PROVIDER_INVALID"},
    )
    return {"value": {"deleted": True}}


async def handle_providers_mapping_dry_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    from arbora.identity.admin_console import dry_run_provider_rules

    providers = await _send(context, "idp", "list", expect="idps")
    provider = next(
        (p for p in providers if p.get("idp_id") == params["provider_id"]), None
    )
    if provider is None:
        raise OperationRefused("PROVIDER_INVALID")
    return {
        "value": dry_run_provider_rules(provider.get("rules") or [], params["claims"])
    }


async def handle_policy_get(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    config = await _send(context, "config", "get", expect="config")
    keys = (
        "epoch",
        "password_min_chars",
        "registration_policy",
        "local_fallback",
        "idle_ms",
        "absolute_ms",
        "privileged_idle_ms",
        "privileged_absolute_ms",
    )
    return {"value": {key: config.get(key) for key in keys}}


async def handle_policy_set(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    request = {k: v for k, v in params.items() if v is not None}
    await _send(context, "config", "update_policy", request, expect="config")
    return await handle_policy_get(context, {}, op)


async def handle_mode_get(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    config = await _send(context, "config", "get", expect="config")
    return {
        "value": {
            "mode": config.get("mode"),
            "epoch": config.get("epoch"),
            "issuer_kid_current": config.get("issuer_kid_current"),
        }
    }


async def handle_mode_transition_step(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    request = {k: v for k, v in params.items() if v is not None}
    try:
        config = await _send(context, "config", "transition", request, expect="config")
    except OperationRefused as refused:
        if refused.details.get("engine_code") in {
            "IDENTITY_EPOCH_CONFLICT",
            "IDENTITY_ILLEGAL_TRANSITION",
            "IDENTITY_PRECONDITION_FAILED",
            "MODE_TRANSITION_FORBIDDEN",
        }:
            raise OperationRefused("STEP_OUT_OF_ORDER", refused.details) from None
        raise
    return {"value": {"mode": config.get("mode"), "epoch": config.get("epoch")}}


async def handle_audit_list(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    request = {"after": params.get("cursor"), "limit": params["limit"]}
    rows = await _send(context, "config", "audit", request, expect="audit")
    page = _page(rows, params["limit"], "seq")
    return {"value": {"events": rows, **page}}


async def handle_audit_verify(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    port = context.services.get("identity_admin")
    if port is None:
        raise RuntimeError("identity admin port is not bound")
    report = await port.audit_verify(context.client, context.caller.engine_claims)
    if not report.get("ok"):
        raise OperationRefused(
            "AUDIT_CHAIN_BROKEN", {"first_broken_seq": report.get("first_broken_seq")}
        )
    return {"value": report}


async def handle_audit_export(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> Any:
    request = {"after": params.get("cursor"), "limit": params["limit"]}
    rows = await _send(context, "config", "audit", request, expect="audit")
    page = _page(rows, params["limit"], "seq")
    events = [
        row for row in rows if params["from_ms"] <= row["at_ms"] <= params["to_ms"]
    ]
    return {
        "value": {
            "events": events,
            "chain_digest": rows[-1]["chain"] if rows else None,
            **page,
        }
    }


_CONSOLE = frozenset({Surface.MCP, Surface.HTTP, Surface.A2A, Surface.CONSOLE})
_ALL = frozenset({Surface.MCP, Surface.HTTP, Surface.A2A})

#: id -> (handler, params, summary, kind) where kind is self, read or admin.
_TABLE: tuple[tuple[str, str, type[_P], str, str], ...] = (
    ("identity.self.get", "handle_self_get", Empty, "Read the caller's own identity record", "self"),
    ("identity.self.sessions.list", "handle_self_sessions_list", Empty, "List the caller's own sessions", "self"),
    ("identity.users.list", "handle_users_list", UsersList, "List users, optionally filtered by text", "read"),
    ("identity.users.get", "handle_users_get", UserRef, "Read one user", "read"),
    ("identity.users.create", "handle_users_create", UserCreate, "Create a user", "admin"),
    ("identity.users.disable", "handle_users_disable", UserRef, "Disable a user and end their sessions", "admin"),
    ("identity.users.enable", "handle_users_enable", UserRef, "Enable a disabled user", "admin"),
    ("identity.users.unlock", "handle_users_unlock", UserRef, "Clear a user's sign-in lock", "admin"),
    ("identity.users.force_logout", "handle_users_force_logout", UserRef, "End every session of a user", "admin"),
    ("identity.users.deprovision", "handle_users_deprovision", UserRef, "Remove a user's access and keep the audit trail", "admin"),
    ("identity.sessions.list", "handle_sessions_list", UserRef, "List the sessions of a user", "read"),
    ("identity.api_keys.revoke", "handle_api_keys_revoke", ApiKeyRevoke, "Revoke an API key", "admin"),
    ("identity.service_accounts.list", "handle_service_accounts_list", PageParams, "List service accounts", "read"),
    ("identity.service_accounts.create", "handle_service_accounts_create", ServiceAccountCreate, "Create a service account", "admin"),
    ("identity.service_accounts.disable", "handle_service_accounts_disable", ServiceAccountRef, "Disable a service account", "admin"),
    ("identity.service_accounts.deprovision", "handle_service_accounts_deprovision", ServiceAccountRef, "Deprovision a service account", "admin"),
    ("identity.roles.list", "handle_roles_list", Empty, "List roles", "read"),
    ("identity.roles.assign", "handle_roles_assign", RoleChange, "Assign a role to a user", "admin"),
    ("identity.roles.remove", "handle_roles_remove", RoleChange, "Remove a role from a user", "admin"),
    ("identity.roles.upsert", "handle_roles_upsert", RoleUpsert, "Create or change a role and its scopes", "admin"),
    ("identity.groups.list", "handle_groups_list", Empty, "List groups", "read"),
    ("identity.groups.upsert", "handle_groups_upsert", GroupUpsert, "Create or change a group", "admin"),
    ("identity.groups.set_members", "handle_groups_set_members", GroupMembers, "Add and remove group members", "admin"),
    ("identity.providers.list", "handle_providers_list", Empty, "List identity providers", "read"),
    ("identity.providers.upsert", "handle_providers_upsert", ProviderUpsert, "Create or change an identity provider", "admin"),
    ("identity.providers.delete", "handle_providers_delete", ProviderRef, "Delete an identity provider", "admin"),
    ("identity.providers.mapping_dry_run", "handle_providers_mapping_dry_run", MappingDryRun, "Preview the roles a provider's rules assign", "read"),
    ("identity.policy.get", "handle_policy_get", Empty, "Read the identity policy", "read"),
    ("identity.policy.set", "handle_policy_set", PolicySet, "Change the identity policy", "admin"),
    ("identity.mode.get", "handle_mode_get", Empty, "Read the authentication mode", "read"),
    ("identity.mode.transition_step", "handle_mode_transition_step", ModeTransition, "Change the authentication mode", "admin"),
    ("identity.audit.list", "handle_audit_list", PageParams, "List identity audit events", "read"),
    ("identity.audit.verify", "handle_audit_verify", Empty, "Verify the audit chain", "read"),
    ("identity.audit.export", "handle_audit_export", AuditExport, "Export identity audit events for a time range", "read"),
)  # fmt: skip


def operations() -> tuple[OpSpec, ...]:
    specs: list[OpSpec] = []
    for op_id, handler, params, summary, kind in _TABLE:
        admin = kind == "admin"
        common: dict[str, Any] = {
            "id": op_id,
            "verb": Verb.MANAGE if admin else Verb.ASK,
            "summary": summary,
            "examples": (summary,),
            "params": params,
            "result": Value,
            "binding": Composite(handler=f"arbora.api.ops.identity.{handler}"),
            "scopes": frozenset({SELF_SCOPE if kind == "self" else ADMIN_SCOPE}),
            "effect": Effect.ADMIN if admin else Effect.READ,
        }
        if kind != "self":
            common["principals"] = PrincipalRule.HUMAN_UNDELEGATED
            common["surfaces"] = _CONSOLE
        if admin:
            common["audit"] = AuditClass.EVENT
        specs.append(OpSpec(**common))
    return tuple(specs)


specs = operations
