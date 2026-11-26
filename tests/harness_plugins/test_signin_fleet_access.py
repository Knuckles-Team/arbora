"""One sign-in gives the connector fleet, scoped to the signed-in principal (R011.1)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.session_loads import SessionLoads

SCOPES = frozenset({"mcp:discover", "mcp:delegate"})


def _caller(principal: str, grants: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        principal=principal, effective_scopes=SCOPES, grants=grants, revoked=False
    )


def _item(server: str) -> CatalogItem:
    return CatalogItem(
        f"fleet:tool:{server}/read",
        "tool",
        "read",
        server=server,
        schema={"type": "object"},
        annotations={"readOnlyHint": True},
    )


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R011.1")
@pytest.mark.asyncio
async def test_one_signin_lists_only_entitled_connectors_and_revocation_stops_calls() -> (
    None
):
    items = (_item("tickets"), _item("ledger"))

    async def source() -> tuple[CatalogItem, ...]:
        return items

    async def entitled(item: CatalogItem, caller: SimpleNamespace) -> bool:
        # The principal's own grants, checked on each call: a revoked session
        # holds none, and no shared connector login is consulted.
        return not caller.revoked and item.server in caller.grants

    async def impossible(*_args: object) -> None:
        raise AssertionError("not called")

    ops = MultiplexerOps(
        catalog=FleetCatalog([source], entitled),
        sessions=SessionLoads(),
        loadable=entitled,
        callable_item=entitled,
        mount=impossible,
        notify=impossible,
        invoke=impossible,
        health=lambda: {},
    )
    alice = _caller("alice", {"tickets", "ledger"})
    bob = _caller("bob", {"tickets"})

    # find lists only the connectors the signed-in principal may load.
    listed = await ops.catalog.search(alice, browse=True)
    assert {i["server"] for i in listed["items"]} == {"tickets", "ledger"}
    listed = await ops.catalog.search(bob, browse=True)
    assert {i["server"] for i in listed["items"]} == {"tickets"}

    # A direct call runs under the principal's grants and prompts for no
    # connector credential.
    assert (await ops.admitted_tool(bob, "tickets", "read")).server == "tickets"

    # A connector the principal lacks answers as an absent one does: the served
    # door flattens both to the same refusal, so nothing reveals it exists.
    with pytest.raises(PermissionError):
        await ops.admitted_tool(bob, "ledger", "read")
    with pytest.raises(PermissionError):
        await ops.admitted_tool(bob, "missing", "read")

    # Revoking the principal's session ends access on the next call.
    alice.revoked = True
    with pytest.raises(PermissionError):
        await ops.admitted_tool(alice, "tickets", "read")
    assert (await ops.catalog.search(alice, browse=True))["items"] == []
