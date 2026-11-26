"""Settings write and the configuration views (ARBORA-OPS-R907)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ops as ops_admin
from arbora.api.registry import AuditClass, Idempotency, PrincipalRule, Surface

pytestmark = pytest.mark.spec("ARBORA-OPS-R907")

_OPS = {op.id: op for op in ops_admin.operations()}


class Store:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, Any, str]] = []
        self.views: list[str] = []

    async def set_config(
        self, *, tenant: str, key: str, value: Any, idempotency_key: str
    ) -> ops_admin.ConfigChange:
        self.set_calls.append((tenant, key, value, idempotency_key))
        if key == "nope":
            raise ops_admin.ConfigRefused("UNKNOWN_SETTING")
        if value == "bad":
            raise ops_admin.ConfigRefused("INVALID_VALUE")
        return ops_admin.ConfigChange(
            tenant=tenant, settings={key: value}, restart_required=key == "port"
        )

    async def config_view(self, *, tenant: str, view: str) -> dict[str, Any]:
        self.views.append(view)
        if view == "secret_status":
            return {
                "secrets": [
                    {
                        "name": "API_KEY",
                        "present": True,
                        "value": "sk-leak",
                        "ref": "x",
                    },
                    {"name": "TOKEN", "present": False},
                ]
            }
        return {"view": view}


def _context(store: object | None, key: str | None = "k") -> Any:
    services = {} if store is None else {"ops_admin_store": store}
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t", principal="p"),
        services=services,
        idempotency_key=key,
    )


def test_set_needs_step_up_and_audits() -> None:
    op = _OPS["ops.config.set"]
    assert op.scopes == frozenset({"ops:admin"})
    assert op.effect.value == "admin"
    assert op.principals is PrincipalRule.HUMAN_UNDELEGATED
    assert Surface.CONSOLE in op.surfaces
    assert op.idempotency is Idempotency.KEY_REQUIRED
    assert op.audit is AuditClass.EVENT


async def test_set_applies_one_setting_and_maps_refusals() -> None:
    store = Store()
    op = _OPS["ops.config.set"]
    done = await ops_admin.handle_ops_config_set(
        _context(store), {"key": "port", "value": 1}, op
    )
    assert done["value"] == {
        "tenant": "t",
        "settings": {"port": 1},
        "restart_required": True,
    }
    for params, code in (
        ({"key": "nope", "value": 1}, "UNKNOWN_SETTING"),
        ({"key": "a", "value": "bad"}, "INVALID_VALUE"),
    ):
        with pytest.raises(OperationRefused) as refused:
            await ops_admin.handle_ops_config_set(_context(store), params, op)
        assert refused.value.code == code
    with pytest.raises(ValueError, match="Idempotency-Key"):
        await ops_admin.handle_ops_config_set(
            _context(store, key=None), {"key": "a", "value": 1}, op
        )
    with pytest.raises(OperationRefused) as unbound:
        await ops_admin.handle_ops_config_set(_context(None), {"key": "a"}, op)
    assert unbound.value.code == "UNAVAILABLE"


async def test_secret_status_returns_presence_only() -> None:
    store = Store()
    op = _OPS["ops.config"]
    answer = await ops_admin.handle_ops_config(
        _context(store), {"view": "secret_status"}, op
    )
    assert answer["value"] == {
        "secrets": [
            {"name": "API_KEY", "present": True},
            {"name": "TOKEN", "present": False},
        ]
    }
    for view in ("groups", "schema"):
        got = await ops_admin.handle_ops_config(_context(store), {"view": view}, op)
        assert got["value"] == {"view": view}
    assert ops_admin.OpsConfigParams().view == "values"
    with pytest.raises(ValueError):
        ops_admin.OpsConfigParams(view="all")  # type: ignore[arg-type]
