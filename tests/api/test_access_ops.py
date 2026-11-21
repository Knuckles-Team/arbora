"""Focused authority tests for access operation handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbora.access import service


def _context(*, principal: str = "approver", delegated: bool = False):
    caller = SimpleNamespace(
        principal=principal,
        tenant="tenant-a",
        delegated=delegated,
        effective_scopes=frozenset({"rbac:approve-elevation"}),
        engine_claims={"agent_id": principal, "tenant": "tenant-a"},
    )
    leases = SimpleNamespace(get=AsyncMock(), list=AsyncMock(), transition=AsyncMock())
    return SimpleNamespace(caller=caller, client=SimpleNamespace(control_leases=leases))


async def test_action_approval_decision_uses_verified_tenant_and_revision() -> None:
    context = _context()
    lease = {"kind": service.ACTION_APPROVAL_KIND, "status": "active", "revision": 7}
    context.client.control_leases.get.return_value = lease
    context.client.control_leases.transition.return_value = {"outcome": "applied"}
    result = await service.grant_approval(
        context, {"approval_id": "action_approval:7"}, None
    )
    assert result == {"outcome": "applied"}
    context.client.control_leases.transition.assert_awaited_once_with(
        tenant="tenant-a",
        lease_id="action_approval:7",
        expected_revision=7,
        to="consumed",
        idempotency_key="decide:action_approval:7",
    )


async def test_action_approval_refuses_wrong_kind_before_transition() -> None:
    context = _context()
    context.client.control_leases.get.return_value = {
        "kind": "capacity.lease",
        "status": "active",
        "revision": 2,
    }
    with pytest.raises(LookupError):
        await service.deny_approval(context, {"approval_id": "action_approval:7"}, None)
    context.client.control_leases.transition.assert_not_awaited()


async def test_elevation_deny_does_not_disguise_revoke() -> None:
    with pytest.raises(NotImplementedError, match="ELEVATION_DENY_UNAVAILABLE"):
        await service.deny_elevation(_context(), {"elevation_id": "e"}, None)


async def test_lease_listing_uses_verified_tenant() -> None:
    context = _context()
    context.client.control_leases.list.return_value = {
        "leases": [],
        "next_cursor": None,
    }
    await service.list_leases(context, {"kind": "action.approval"}, None)
    context.client.control_leases.list.assert_awaited_once_with(
        tenant="tenant-a", kind="action.approval", status=None, cursor=None, limit=100
    )


def test_operations_require_console_confirmation_for_decisions() -> None:
    pytest.importorskip("arbora.api.registry")
    from arbora.api.ops.access import operations

    ops = {op.id: op for op in operations()}
    assert "access.elevation.deny" not in ops
    # approvals.* stay unregistered until EG publishes approvals:read/decide
    # (ARBORA-IDENTITY-R019 follow-up); service.py still implements them.
    assert "approvals.grant" not in ops
    assert "approvals.deny" not in ops
    op = ops["access.elevation.approve"]
    assert op.confirm is not None
    assert op.confirm.value == "console"
    assert op.principals.value == "human_undelegated"


@pytest.mark.spec("ARBORA-IDENTITY-R001.1")
def test_generated_schema_types_elevation_params_result_receipt_and_refusals() -> None:
    from arbora.api.errors import ArboraErrorCode
    from arbora.api.ops.access import operations

    ops = {op.id: op for op in operations()}
    request = ops["access.elevation.request"].params.model_json_schema()
    assert set(request["properties"]) == {
        "scopes",
        "span_ms",
        "justification",
        "elevation_id",
    }
    assert request["required"] == ["scopes", "span_ms", "justification"]
    assert request["properties"]["scopes"]["maxItems"] == 16
    assert request["properties"]["justification"]["maxLength"] == 2048
    assert "path" not in request["properties"]
    approve = ops["access.elevation.approve"].params.model_json_schema()
    assert set(approve["properties"]) == {"elevation_id", "request_digest"}
    assert set(
        ops["access.elevation.revoke"].params.model_json_schema()["properties"]
    ) == {"elevation_id"}
    assert not ops["access.elevation.list"].params.model_json_schema()["properties"]
    for name in ("request", "approve", "revoke"):
        result = ops[f"access.elevation.{name}"].result.model_json_schema()
        assert {"elevation_id", "request_digest", "remaining_ms", "own"} <= set(
            result["properties"]
        )
        assert "receipt" in result["required"]
    listing = ops["access.elevation.list"].result.model_json_schema()
    assert set(listing["properties"]) == {"elevations"}
    for code in (
        "ELEVATION_STALE_VIEW",
        "ELEVATION_APPROVAL_SURFACE",
        "ELEVATION_APPROVER_DELEGATED",
        "ELEVATION_APPROVER_SCOPE",
    ):
        assert ArboraErrorCode(code)


@pytest.mark.spec("ARBORA-IDENTITY-R001.1")
async def test_elevation_write_returns_the_view_with_a_receipt(monkeypatch) -> None:
    from arbora.security.elevation import ElevationScope, ElevationView

    view = ElevationView(
        elevation_id="e1",
        grantee="alice",
        status="requested",
        scopes=[ElevationScope(graph="g", action="read")],
        span_ms=1000,
        justification="why",
        request_digest="d" * 8,
        requested_at_ms=1,
        revision=1,
    )
    fake = SimpleNamespace(request=AsyncMock(return_value=view))
    monkeypatch.setattr(service, "_elevations", lambda _context: fake)
    context = _context()
    context.audit_ref = '["ref", 3]'
    result = await service.request_elevation(
        context,
        {
            "scopes": [{"graph": "g", "action": "read"}],
            "span_ms": 1000,
            "justification": "why",
        },
        None,
    )
    assert result["receipt"] == {"audit_ref": '["ref", 3]', "request_digest": "d" * 8}
    assert result["elevation_id"] == "e1"


@pytest.mark.spec("ARBORA-IDENTITY-R001.1")
async def test_surface_refusal_keeps_its_stable_code(monkeypatch) -> None:
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.security.elevation import ElevationRefused

    fake = SimpleNamespace(
        approve=AsyncMock(side_effect=ElevationRefused("ELEVATION_STALE_VIEW", "x"))
    )
    monkeypatch.setattr(service, "_elevations", lambda _context: fake)
    context = _context()
    context.caller.engine_claims = {"agent_id": "approver"}
    with pytest.raises(OperationRefused) as raised:
        await service.approve_elevation(
            context, {"elevation_id": "e1", "request_digest": "d"}, None
        )
    assert raised.value.code == "ELEVATION_STALE_VIEW"
