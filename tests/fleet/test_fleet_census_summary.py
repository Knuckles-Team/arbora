"""The paged fleet census and the per-server read share one snapshot.

ARBORA-FLEET-R040.3: ``fleet.catalog.list`` with ``summary`` pages every
registered connector by server name (a server with no prompts, or with no
items at all, is still listed); ``find(op="catalog", params={server})`` reads
the same snapshot and answers ``NOT_FOUND`` with the pack status for a
server the snapshot lacks; a connector redeploy, seen as a changed verified
record, drops the stale probed schema.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.mcp.discovery import _fleet_catalog
from arbora.api.ops.fleet import handle_fleet_operation, operations
from arbora.api.registry import Registry
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.catalog_sources import CombinedFleetSource
from arbora.fleet.coverage import registered_connectors
from arbora.fleet.gateway_ops import (
    ConfirmingTools,
    FleetGateway,
    tool_for_multiplexer_ops,
)
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.session_loads import SessionLoads

pytestmark = [pytest.mark.asyncio, pytest.mark.spec("ARBORA-FLEET-R040.3")]

REGISTRY = Registry(operations())


def _manifest(servers: list[str]) -> dict[str, Any]:
    repositories = [
        {"url": f"https://github.com/example/{name}.git", "mcp_server": name}
        for name in servers
    ]
    return {
        "subdirectories": {
            "arbora": {"subdirectories": {"connectors": {"repositories": repositories}}}
        }
    }


def _served(tmp_path, source, *, registered, unadmitted=(), health=None):
    path = tmp_path / "workspace.yml"
    path.write_text(yaml.safe_dump(_manifest(registered)), encoding="utf-8")

    async def allowed(_item, _caller, _extra=None):
        return True

    async def never(*_args, **_kwargs):
        raise AssertionError("this test does not mount or call a child")

    async def notify(_key):
        return True

    ops = MultiplexerOps(
        catalog=FleetCatalog([source], allowed),
        sessions=SessionLoads(),
        loadable=allowed,
        mount=never,
        notify=notify,
        invoke=never,
        health=lambda: dict(health or {}),
        coverage_registry=lambda: registered_connectors(path),
        coverage_endpoints=lambda: (),
        unadmitted_servers=lambda: list(unadmitted),
    )
    gateway = FleetGateway(
        tool_for=tool_for_multiplexer_ops(ops),
        policy_check=allowed,
        delegated_call=never,
        catalog_ops=ops,
        confirming_tools=ConfirmingTools(),
    )
    caller = SimpleNamespace(
        effective_scopes=frozenset({"mcp:discover"}),
        tenant="tenant",
        principal="alice",
        session=None,
    )
    return SimpleNamespace(
        gateway=gateway,
        caller=caller,
        context=SimpleNamespace(caller=caller, services={"fleet_gateway": gateway}),
    )


def _rows(*items: CatalogItem):
    async def source():
        return items

    return source


async def _list(served, **params):
    op = REGISTRY.get("fleet.catalog.list")
    return (await handle_fleet_operation(served.context, params, op))["value"]


def _tools(server: str, count: int) -> list[CatalogItem]:
    return [
        CatalogItem(f"fleet:tool:{server}/t{i}", "tool", f"t{i}", server=server)
        for i in range(count)
    ]


async def test_summary_pages_every_connector_with_pack_status(tmp_path) -> None:
    source = _rows(
        *_tools("alpha", 3),
        CatalogItem("fleet:prompt:alpha/guide", "prompt", "guide", server="alpha"),
        *_tools("beta", 2),  # beta serves no prompt
        *_tools("gamma", 1),
    )
    served = _served(
        tmp_path,
        source,
        registered=["alpha", "beta", "gamma", "delta"],
        unadmitted=("gamma",),
        health={"beta": {"catalog_refresh_error": "schema_refresh_failed"}},
    )
    first = await _list(served, summary=True, limit=2)
    assert first["items"] == []
    assert [row["server"] for row in first["servers"]] == ["alpha", "beta"]
    second = await _list(served, summary=True, limit=2, cursor=first["next_cursor"])
    assert [row["server"] for row in second["servers"]] == ["delta", "gamma"]
    assert second["next_cursor"] is None

    rows = {row["server"]: row for row in [*first["servers"], *second["servers"]]}
    assert rows["alpha"]["surfaces"]["tools"]["count"] == 3
    assert rows["alpha"]["surfaces"]["prompts"]["status"] == "served"
    assert rows["beta"]["surfaces"]["tools"]["count"] == 2
    assert rows["beta"]["surfaces"]["prompts"]["status"] == "not served"
    assert "prompts" in rows["beta"]["surface_gap"]
    assert rows["beta"]["probe_error"] == "schema_refresh_failed"
    assert {name: row["pack_status"] for name, row in rows.items()} == {
        "alpha": "served",
        "beta": "served",
        "gamma": "registered_unpublished",
        "delta": "registered_unpublished",
    }
    assert rows["delta"]["surfaces"]["tools"]["count"] == 0


async def test_per_server_read_matches_the_catalog_snapshot(tmp_path) -> None:
    source = _rows(*_tools("craft", 120), *_tools("other", 1))
    served = _served(tmp_path, source, registered=["craft", "other", "absent"])
    census = await _list(served, summary=True)
    counted = {row["server"]: row for row in census["servers"]}["craft"]

    found = await _fleet_catalog(
        served.gateway.catalog, served.caller, {"server": "craft"}, REGISTRY
    )
    catalog = found["catalog"]
    assert catalog["server"] == "craft"
    assert len(catalog["tools"]) == counted["surfaces"]["tools"]["count"] == 120
    assert catalog["pack_status"] == "served"

    with pytest.raises(OperationRefused) as refused:
        await _fleet_catalog(
            served.gateway.catalog, served.caller, {"server": "absent"}, REGISTRY
        )
    assert refused.value.code == "NOT_FOUND"
    assert refused.value.details == {
        "server": "absent",
        "pack_status": "registered_unpublished",
    }


def _snapshot(schema: dict[str, Any]) -> Any:
    tool = SimpleNamespace(
        entry=SimpleNamespace(
            kind="tool", server_name="craft", upstream_name="edit", summary="Edit"
        ),
        content=SimpleNamespace(body=json.dumps({"input_schema": schema}).encode()),
    )
    server = SimpleNamespace(
        component=SimpleNamespace(server_name="craft"),
        registration=object(),
        content=SimpleNamespace(body=b"{}"),
        provides=(tool,),
    )
    return SimpleNamespace(servers=(server,))


async def test_connector_redeploy_refreshes_its_schemas(tmp_path) -> None:
    old = {"type": "object", "properties": {"title": {"type": "string"}}}
    new = {"type": "object", "properties": {"slug": {"type": "string"}}}
    state = {"snapshot": _snapshot(old)}
    probe_cache: dict[str, Any] = {
        "craft": {"tools": [{"name": "edit", "inputSchema": old}]}
    }

    async def verified():
        return state["snapshot"]

    async def live():
        return dict(probe_cache)

    async def sdk():
        return ()

    source = CombinedFleetSource(
        verified=verified,
        live=live,
        sdk=sdk,
        on_redeploy=lambda server: probe_cache.pop(server, None),
    )
    served = _served(tmp_path, source, registered=["craft"])

    async def schema() -> dict[str, Any]:
        found = await _fleet_catalog(
            served.gateway.catalog, served.caller, {"server": "craft"}, REGISTRY
        )
        (tool,) = found["catalog"]["tools"]
        return tool["input_schema"]

    assert await schema() == old
    state["snapshot"] = _snapshot(new)  # the connector redeploys
    assert await schema() == new
    assert "craft" not in probe_cache  # the stale probe is dropped
