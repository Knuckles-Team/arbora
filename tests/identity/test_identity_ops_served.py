"""Admin gate of the identity operations through registry invoke (ARBORA-IDENTITY-R018.6)."""

from __future__ import annotations

import time

import pytest

from arbora.api.ops import identity
from arbora.api.registry import Registry, Surface
from tests.api.test_invoke_contract import caller, run, setup_services

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R018.6")

_ADMIN = frozenset({"identity:admin"})


async def _answer() -> dict:
    return {"value": {}}


def _services():
    parts = setup_services(
        registry=Registry(identity.operations()),
        external_effect_operations=frozenset({"identity.users.disable"}),
    )
    parts[1].on_dispatch = _answer
    return parts


def _admin_caller(**changes):
    return caller(
        effective_scopes=_ADMIN,
        credential_kind="session",
        mfa_at_ms=int(time.time() * 1000),
        **changes,
    )


def test_admin_read_off_the_console_runs_no_handler() -> None:
    services, runtime, _, _, _ = _services()
    result = run(
        services,
        op_id="identity.users.list",
        params={},
        supplied=_admin_caller(),
        surface=Surface.HTTP,
    )
    assert result.code == "CONSOLE_ORIGIN_REQUIRED"
    assert runtime.calls == []


def test_admin_read_on_the_console_reaches_the_handler() -> None:
    services, runtime, _, _, _ = _services()
    result = run(
        services,
        op_id="identity.users.list",
        params={},
        supplied=_admin_caller(),
        surface=Surface.CONSOLE,
    )
    assert result.code == "OK"
    assert len(runtime.calls) == 1


def test_missing_admin_scope_runs_no_handler() -> None:
    services, runtime, _, _, _ = _services()
    result = run(
        services,
        op_id="identity.users.list",
        params={},
        supplied=caller(effective_scopes=frozenset({"identity:self"})),
        surface=Surface.CONSOLE,
    )
    assert result.code == "SCOPE_REQUIRED"
    assert runtime.calls == []


def test_admin_mutation_with_a_stale_second_factor_runs_no_handler() -> None:
    services, runtime, _, _, _ = _services()
    fresh = _admin_caller()
    plan = run(
        services,
        op_id="identity.users.disable",
        params={"user_id": "u"},
        supplied=fresh,
        surface=Surface.CONSOLE,
    )
    assert plan.code == "STEP_UP_REQUIRED", plan
    stale = _admin_caller()
    object.__setattr__(stale, "mfa_at_ms", int(time.time() * 1000) - 600_000)
    result = run(
        services,
        op_id="identity.users.disable",
        params={"user_id": "u"},
        supplied=stale,
        surface=Surface.CONSOLE,
        plan_ref=plan.details["plan_ref"],
    )
    assert result.code == "MFA_STALE"
    assert runtime.calls == []


def test_self_operations_need_no_console() -> None:
    services, runtime, _, _, _ = _services()
    result = run(
        services,
        op_id="identity.self.get",
        params={},
        supplied=caller(effective_scopes=frozenset({"identity:self"})),
        surface=Surface.HTTP,
    )
    assert result.code == "OK"
    assert len(runtime.calls) == 1


pytestmark = [
    pytest.mark.spec("ARBORA-IDENTITY-R018.6"),
    pytest.mark.spec("ARBORA-IDENTITY-R018.7"),
]


class _TenantPort:
    """A fake engine whose rows belong to a tenant; it reads the forwarded claims."""

    def __init__(self) -> None:
        self.rows = {
            "tenant:one": [{"principal_id": "usr:one", "username": "one"}],
            "tenant:two": [{"principal_id": "usr:two", "username": "two"}],
        }
        self.seen: list[tuple[str, object]] = []

    async def identity(self, client, claims, family, op, request=None, *, expect):
        self.seen.append((claims["tenant"], request))
        rows = self.rows[claims["tenant"]]
        if op == "get":
            found = [r for r in rows if r["principal_id"] == request["id"]]
            if not found:
                from arbora.identity.engine_admin import IdentityRefused

                raise IdentityRefused("IDENTITY_NOT_FOUND")
            return found[0]
        return rows

    async def audit_verify(self, client, claims):
        return {"ok": True}


def _serving(port: _TenantPort):
    import types

    parts = setup_services(registry=Registry(identity.operations()), effects=None)
    services, runtime = parts[0], parts[1]

    async def dispatch(op, params, context):
        from arbora.api.ops import identity as module

        handler = getattr(module, op.binding.handler.rpartition(".")[2])
        bound = types.SimpleNamespace(
            caller=context.caller,
            client=object(),
            services={"identity_admin": port},
        )
        return await handler(bound, params, op)

    runtime.dispatch = dispatch
    return services, runtime


def _tenant_caller(tenant: str, scopes: frozenset[str], **changes):
    return caller(
        tenant=tenant,
        principal=f"usr:{tenant[-3:]}",
        effective_scopes=scopes,
        engine_claims={
            "principal": f"usr:{tenant[-3:]}",
            "tenant": tenant,
            "scopes": sorted(scopes),
            "policy_version": "revision:one",
            "delegation": False,
        },
        **changes,
    )


def test_http_and_mcp_return_identical_results_and_refusals() -> None:
    port = _TenantPort()
    services, _ = _serving(port)
    own = _tenant_caller("tenant:one", frozenset({"identity:self"}))
    results = [
        run(
            services,
            op_id="identity.self.sessions.list",
            params={},
            supplied=own,
            surface=s,
        )
        for s in (Surface.HTTP, Surface.MCP)
    ]
    assert results[0] == results[1]
    admin = _tenant_caller(
        "tenant:one",
        _ADMIN,
        credential_kind="session",
        mfa_at_ms=int(time.time() * 1000),
    )
    refusals = [
        run(services, op_id="identity.users.list", params={}, supplied=admin, surface=s)
        for s in (Surface.HTTP, Surface.MCP)
    ]
    assert refusals[0] == refusals[1]
    assert refusals[0].code == "CONSOLE_ORIGIN_REQUIRED"
    missing = [
        run(services, op_id="identity.users.list", params={}, supplied=own, surface=s)
        for s in (Surface.HTTP, Surface.MCP)
    ]
    assert missing[0] == missing[1]
    assert missing[0].code == "SCOPE_REQUIRED"


def test_a_caller_never_reads_another_tenants_rows() -> None:
    port = _TenantPort()
    services, _ = _serving(port)
    admin_one = _tenant_caller(
        "tenant:one",
        _ADMIN,
        credential_kind="session",
        mfa_at_ms=int(time.time() * 1000),
    )
    admin_two = _tenant_caller(
        "tenant:two",
        _ADMIN,
        credential_kind="session",
        mfa_at_ms=int(time.time() * 1000),
    )
    one = run(
        services,
        op_id="identity.users.list",
        params={},
        supplied=admin_one,
        surface=Surface.CONSOLE,
    )
    two = run(
        services,
        op_id="identity.users.list",
        params={},
        supplied=admin_two,
        surface=Surface.CONSOLE,
    )
    assert [u["username"] for u in one.value["value"]["users"]] == ["one"]
    assert [u["username"] for u in two.value["value"]["users"]] == ["two"]
    foreign = run(
        services,
        op_id="identity.users.get",
        params={"user_id": "usr:two"},
        supplied=admin_one,
        surface=Surface.CONSOLE,
    )
    assert foreign.code == "USER_NOT_FOUND"
    assert all("tenant" not in (req or {}) for _, req in port.seen)
