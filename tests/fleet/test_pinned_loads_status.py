"""find and status show the pinned tools; unload names the skipped pins.

ARBORA-FLEET-R059.7: a search flags each item the caller has pinned, the
session status reports the pinned cap, usage and list, and the status lists
the suspended pins from the durable store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.pin_store import InMemoryPinStore
from arbora.fleet.session_loads import SessionLoads


def _caller() -> Any:
    return SimpleNamespace(
        effective_scopes=frozenset({"mcp:discover", "mcp:delegate"}),
        tenant="tenant",
        principal="alice",
    )


async def _harness() -> SimpleNamespace:
    rows = (
        CatalogItem("fleet:tool:s/read", "tool", "read", server="s"),
        CatalogItem("fleet:tool:s/secret", "tool", "secret", server="s"),
    )

    async def source():
        return rows

    async def yes(_item, _caller):
        return True

    async def mount(item, call):
        return None

    async def notify(key):
        return True

    async def invoke(op, params, current, surface):
        raise AssertionError("this test does not call a child")

    store = InMemoryPinStore()
    ops = MultiplexerOps(
        catalog=FleetCatalog([source], yes),
        sessions=SessionLoads(),
        loadable=yes,
        mount=mount,
        notify=notify,
        invoke=invoke,
        health=lambda: {},
        session_key_for=lambda caller: f"{caller.tenant}:{caller.principal}",
        pin_store=store,
        coverage_registry=lambda: (),
        coverage_endpoints=lambda: (),
        unadmitted_servers=lambda: [],
    )
    return SimpleNamespace(ops=ops, store=store, caller=_caller())


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.7")
async def test_find_and_status_flag_pinned_items() -> None:
    state = await _harness()
    ops = state.ops
    caller = state.caller
    key = "tenant:alice"

    await ops.pin(caller, ["fleet:tool:s/read"])
    assert ops.sessions.pinned_items(key) == {"fleet:tool:s/read"}

    # find flags the pinned row only
    found = await ops.find_tools(caller, browse=True)
    rows = {row["id"]: row for row in found["items"]}
    assert rows["fleet:tool:s/read"]["pinned"] is True
    assert "pinned" not in rows["fleet:tool:s/secret"]

    # status reports the pinned cap, usage, list, and no suspensions
    status = await ops.status(caller)
    session = status["session"]
    assert session["pinned_cap"] == 32
    assert session["pinned_used"] == 1
    assert session["pinned"] == ["fleet:tool:s/read"]
    assert session["used"] == 1
    assert status["suspended"] == []

    # a selector unload skips the pinned item and names it
    result = await ops.unload(caller, kinds=["tool"])
    assert result["skipped_pinned"] == ["fleet:tool:s/read"]
    assert result["unloaded"] == []
    assert result["session_total"] == 1
    assert ops.sessions.pinned_items(key) == {"fleet:tool:s/read"}

    # an explicit item unload removes the pin, durably
    result = await ops.unload(caller, items=["fleet:tool:s/read"])
    assert result["unloaded"] == ["fleet:tool:s/read"]
    assert result["skipped_pinned"] == []
    assert ops.sessions.pinned_items(key) == frozenset()
    assert await state.store.list("tenant", "alice") == ()

    # a pin that fails the re-check shows up suspended
    await ops.pin(caller, ["fleet:tool:s/read"])

    async def deny(_item, _caller):
        return False

    ops._loadable = deny
    status = await ops.status(caller)
    assert status["suspended"] == [
        {"item_id": "fleet:tool:s/read", "code": "POLICY_DENIED"}
    ]
