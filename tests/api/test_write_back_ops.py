"""ARBORA-FLEET-R003.2: write-back is a typed, gated registry operation."""

from __future__ import annotations

import pytest

from arbora.api.ops import write_back
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Confirm, Idempotency, Registry, Surface
from tests.api._ops_support import service_context

_PARAMS = {
    "connector": "sql",
    "target": "customers.email",
    "change": {"value": "a@example.com"},
    "idempotency_key": "wb-1",
}


def _ctx(calls: list):
    def effect(connector, target, change):
        calls.append((connector, target, change))
        return {"id": len(calls)}

    return service_context(
        write_back_ledger=write_back.WriteBackLedger(), write_back_effect=effect
    )


@pytest.mark.spec("ARBORA-FLEET-R003.2")
@pytest.mark.asyncio
async def test_preview_then_commit_and_idempotent_replay_returns_original_receipt() -> (
    None
):
    calls: list = []
    ctx = _ctx(calls)
    ops = {o.id: o for o in write_back.operations()}
    preview = await write_back.handle_write_back_preview(
        ctx, _PARAMS, ops["fleet.write_back.preview"]
    )
    assert preview["value"]["applied"] is False and calls == []
    first = await write_back.handle_write_back_commit(
        ctx, _PARAMS, ops["fleet.write_back.commit"]
    )
    again = await write_back.handle_write_back_commit(
        ctx, _PARAMS, ops["fleet.write_back.commit"]
    )
    assert first == again and len(calls) == 1
    assert first["value"]["receipt"] == {"id": 1}


@pytest.mark.spec("ARBORA-FLEET-R003.2")
@pytest.mark.asyncio
async def test_commit_without_matching_preview_or_with_mismatch_is_refused() -> None:
    calls: list = []
    ctx = _ctx(calls)
    commit_op = write_back.operations()[1]
    with pytest.raises(write_back.WriteBackRefused):
        await write_back.handle_write_back_commit(ctx, _PARAMS, commit_op)
    await write_back.handle_write_back_preview(ctx, _PARAMS, write_back.operations()[0])
    with pytest.raises(write_back.WriteBackRefused):
        await write_back.handle_write_back_commit(
            ctx, {**_PARAMS, "change": {"value": "other"}}, commit_op
        )
    with pytest.raises(write_back.WriteBackRefused):
        await write_back.handle_write_back_commit(
            ctx, {**_PARAMS, "idempotency_key": "wb-2"}, commit_op
        )
    assert calls == []


@pytest.mark.spec("ARBORA-FLEET-R003.2")
def test_operations_are_registered_gated_and_not_native_child_tools() -> None:
    registry = get_registry()
    commit = registry["fleet.write_back.commit"]
    assert registry["fleet.write_back.preview"]
    assert commit.confirm is Confirm.PLAN
    assert commit.idempotency is Idempotency.KEY_REQUIRED
    assert Surface.MCP in commit.surfaces
    assert isinstance(registry, Registry)
