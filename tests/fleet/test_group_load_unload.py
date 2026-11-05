"""Load and unload the tools of one connector group.

ARBORA-FLEET-R061: a connector group is a lowercase word of 2 to 24 letters
and digits; a load by group resolves to the admitted, loadable tool items of
the group and follows the ordinary item path; an unload by group leaves the
other groups loaded and skips pinned items; a group over the session cap
refuses and loads nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.pin_store import InMemoryPinStore
from arbora.fleet.session_loads import SessionLoads

_ALPHAS = (
    CatalogItem("fleet:tool:s/alpha1", "tool", "alpha1", server="s", group="alpha"),
    CatalogItem("fleet:tool:s/alpha2", "tool", "alpha2", server="s", group="alpha"),
)
_BETAS = (CatalogItem("fleet:tool:s/beta1", "tool", "beta1", server="s", group="beta"),)
_PLAIN = (CatalogItem("fleet:tool:s/plain", "tool", "plain", server="s"),)


def _caller() -> Any:
    return SimpleNamespace(
        effective_scopes=frozenset({"mcp:discover", "mcp:delegate"}),
        tenant="tenant",
        principal="alice",
    )


async def _harness(*, sessions: SessionLoads | None = None) -> SimpleNamespace:
    rows = (*_ALPHAS, *_BETAS, *_PLAIN)

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
        sessions=sessions or SessionLoads(),
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
@pytest.mark.spec("ARBORA-FLEET-R061")
async def test_group_load_and_unload_by_namespace() -> None:
    state = await _harness()
    ops = state.ops
    caller = state.caller
    key = "tenant:alice"

    # a load by group loads only that group
    result = await ops.load(caller, items=[], groups=["alpha"])
    assert sorted(item["id"] for item in result["items"]) == [
        "fleet:tool:s/alpha1",
        "fleet:tool:s/alpha2",
    ]
    assert ops.sessions.loaded(key) == {
        "fleet:tool:s/alpha1",
        "fleet:tool:s/alpha2",
    }

    # a second group joins the session; find filters by group
    await ops.load(caller, items=[], groups=["beta"])
    assert ops.sessions.loaded(key) == {
        "fleet:tool:s/alpha1",
        "fleet:tool:s/alpha2",
        "fleet:tool:s/beta1",
    }
    found = await ops.find_tools(caller, groups=("alpha",))
    assert [row["id"] for row in found["items"]] == [
        "fleet:tool:s/alpha1",
        "fleet:tool:s/alpha2",
    ]
    assert all(row.get("group") == "alpha" for row in found["items"])

    # an unknown group is an empty result, never an error
    result = await ops.load(caller, items=[], groups=["gamma"])
    assert result["items"] == []
    assert result["loaded"] == []
    assert ops.sessions.loaded(key) == {
        "fleet:tool:s/alpha1",
        "fleet:tool:s/alpha2",
        "fleet:tool:s/beta1",
    }

    # a group of another form is refused with the stable code
    for bad in ("Alpha", "a", "alpha_x", "alpha alpha", "a" * 25):
        with pytest.raises(OperationRefused) as refused:
            await ops.load(caller, items=[], groups=[bad])
        assert refused.value.code == "FLEET_GROUP_INVALID"
        assert refused.value.details.get("group") == bad

    # an item load and a group load are one atomic request
    result = await ops.load(caller, items=["fleet:tool:s/plain"], groups=["beta"])
    assert sorted(item["id"] for item in result["items"]) == [
        "fleet:tool:s/beta1",
        "fleet:tool:s/plain",
    ]

    # pin alpha1; an unload by group skips it and keeps the other loaded
    await ops.pin(caller, ["fleet:tool:s/alpha1"])
    result = await ops.unload(caller, groups=["alpha"])
    assert result["skipped_pinned"] == ["fleet:tool:s/alpha1"]
    assert result["unloaded"] == ["fleet:tool:s/alpha2"]
    assert ops.sessions.loaded(key) == {
        "fleet:tool:s/alpha1",
        "fleet:tool:s/beta1",
        "fleet:tool:s/plain",
    }

    # with include_pinned the group unload takes the pinned item too
    result = await ops.unload(caller, groups=["alpha"], include_pinned=True)
    assert result["unloaded"] == ["fleet:tool:s/alpha1"]
    assert result["skipped_pinned"] == []
    assert ops.sessions.pinned_items(key) == frozenset()
    assert await state.store.list("tenant", "alice") == ()

    # a load needs items or groups: the empty request is refused
    from arbora.api.ops.fleet import handle_fleet_operation, operations
    from arbora.api.registry import Registry

    op = Registry(operations()).get("fleet.tools.load")
    assert op is not None
    context = SimpleNamespace(
        caller=caller, services={"fleet_gateway": SimpleNamespace()}
    )
    with pytest.raises(OperationRefused) as refused:
        await handle_fleet_operation(context, {"items": [], "groups": []}, op)
    assert refused.value.code == "INVALID_ARGUMENT"
    assert refused.value.details == {"reason": "a load needs items or groups"}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R061")
async def test_group_larger_than_the_session_cap_refuses_and_loads_nothing() -> None:
    # 70 tool items in one group against a session cap of 64
    rows = tuple(
        CatalogItem(
            f"fleet:tool:s/bulk{i}", "tool", f"bulk{i}", server="s", group="bulk"
        )
        for i in range(70)
    )

    async def source():
        return rows

    async def yes(_item, _caller):
        return True

    async def mount(item, call):
        return None

    async def notify(key):
        return True

    ops = MultiplexerOps(
        catalog=FleetCatalog([source], yes),
        sessions=SessionLoads(cap=64),
        loadable=yes,
        mount=mount,
        notify=notify,
        invoke=lambda *args: (_ for _ in ()).throw(AssertionError),
        health=lambda: {},
        session_key_for=lambda caller: "tenant:alice",
    )
    caller = _caller()

    with pytest.raises(OperationRefused) as refused:
        await ops.load(caller, items=[], groups=["bulk"])
    assert refused.value.code == "LOAD_CAP_EXCEEDED"
    assert refused.value.details.get("cap") == "session"
    # atomic: nothing was loaded
    assert ops.sessions.loaded("tenant:alice") == frozenset()
