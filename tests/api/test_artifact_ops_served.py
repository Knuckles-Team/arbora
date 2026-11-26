"""Artifact operations serve create, get and refresh (ARBORA-OPS-R881)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import artifacts
from tests.api._ops_support import tenant_store_context


class _Store:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    async def create(self, *, tenant, template, source):
        row = {"artifact_id": f"a{len(self.rows)}", "template": template, "version": 0}
        self.rows[(tenant, row["artifact_id"])] = row
        return row

    async def get(self, *, tenant, artifact_id):
        return self.rows.get((tenant, artifact_id))

    async def refresh(self, *, tenant, artifact_id):
        row = self.rows.get((tenant, artifact_id))
        if row is not None:
            row["version"] += 1
        return row


@pytest.mark.spec("ARBORA-OPS-R881")
async def test_artifact_operations_serve_over_http_and_mcp() -> None:
    ops = {o.id: o for o in artifacts.operations()}
    assert ops["artifact.get"].scopes == {"blob:read"}
    assert (
        ops["artifact.create"].scopes
        == ops["artifact.refresh"].scopes
        == {"blob:write"}
    )
    assert set(artifacts.NATIVE_IDEMPOTENCY) == {"artifact.create", "artifact.refresh"}
    context = tenant_store_context(store=_Store(), service_name="artifact_store")
    made = await artifacts.handle_artifact_create(
        context, {"template": "t", "source": {}}, ops["artifact.create"]
    )
    ref = {"artifact_id": made["value"]["artifact_id"]}
    assert (await artifacts.handle_artifact_get(context, ref, ops["artifact.get"]))[
        "value"
    ]["version"] == 0
    refreshed = await artifacts.handle_artifact_refresh(
        context, ref, ops["artifact.refresh"]
    )
    assert refreshed["value"]["version"] == 1
    for handler, op_id in (
        (artifacts.handle_artifact_get, "artifact.get"),
        (artifacts.handle_artifact_refresh, "artifact.refresh"),
    ):
        with pytest.raises(OperationRefused) as absent:
            await handler(context, {"artifact_id": "nope"}, ops[op_id])
        assert absent.value.code == "NOT_FOUND"
    bare = tenant_store_context(store=None, service_name="artifact_store")
    with pytest.raises(OperationRefused) as down:
        await artifacts.handle_artifact_get(bare, ref, ops["artifact.get"])
    assert down.value.code == "UNAVAILABLE"
