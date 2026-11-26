"""Ecosystem widget reads from connector pack data (ARBORA-OPS-R914)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ecosystem
from tests.api._engine_double import context

pytestmark = pytest.mark.spec("ARBORA-OPS-R914")

_READ = {"readOnlyHint": True}
#: server -> tool rows the pack catalog holds (name, required args, hints).
_PACKS: dict[str, list[tuple[str, list[str], dict[str, Any]]]] = {
    "arr-mcp": [
        ("add_movie", [], {}),
        ("queue", ["id"], _READ),
        ("status", [], _READ),
        ("health", [], {**_READ, "widget": True}),
    ],
    "kafka-mcp": [("topics", [], _READ)],
    "writes-only": [("create", [], {})],
}
_RESULTS: dict[tuple[str, str], Any] = {
    ("arr-mcp", "health"): {"structuredContent": {"items": [{"name": "radarr"}]}},
    ("kafka-mcp", "topics"): {"count": 3},
}


class Fleet:
    def __init__(self) -> None:
        self.down: set[str] = set()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __call__(self, _ctx: Any, op_id: str, arguments: dict[str, Any]) -> Any:
        if op_id == "fleet.status":
            return {"value": {"children": {"c1": {"state": "up"}}, "servers": []}}
        if op_id == "fleet.catalog.list" and arguments.get("summary"):
            return {"value": {"servers": [{"server": s} for s in _PACKS]}}
        if op_id == "fleet.catalog.list":
            (server,) = arguments["servers"]
            return {
                "value": {
                    "items": [
                        {
                            "id": f"{server}/{name}",
                            "name": name,
                            "input_schema": {"required": required},
                        }
                        for name, required, _hints in _PACKS[server]
                    ]
                }
            }
        assert op_id == "fleet.call"
        server, tool = arguments["server"], arguments["tool"]
        self.calls.append((server, tool, arguments["arguments"]))
        if server in self.down:
            raise ConnectionError(server)
        return {"value": _RESULTS[(server, tool)]}


class Catalog:
    async def get(self, item_id: str, _caller: Any) -> Any:
        server, name = item_id.split("/")
        hints = next(h for n, _r, h in _PACKS[server] if n == name)
        return SimpleNamespace(annotations=hints or None)


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch) -> Fleet:
    fake = Fleet()
    monkeypatch.setattr(ecosystem, "invoke_registered", fake)
    ecosystem._last_rows.clear()
    return fake


def _ctx(tenant: str = "tenant:one") -> Any:
    base = context(tenant)
    base.services = {"fleet_gateway": SimpleNamespace(catalog=Catalog())}
    return base


async def _read(widget: str, tenant: str = "tenant:one") -> dict[str, Any]:
    return await ecosystem.handle_widget_read(
        _ctx(tenant),
        {"widget": widget, "params": {"limit": 5}},
        None,  # type: ignore[arg-type]
    )


async def test_each_widget_projects_rows_from_its_connector_call(fleet: Fleet) -> None:
    arr = await _read("arr_mcp")
    assert arr["source"] == "arr_mcp" and arr["degraded"] is False
    assert arr["rows"] == [{"name": "radarr"}] and arr["fetched_at"]
    # The pack's widget hint wins over the first read-only tool by name, and a
    # tool that needs an argument or writes is never a widget tool.
    assert fleet.calls == [("arr-mcp", "health", {"limit": 5})]
    kafka = await _read("kafka_mcp")
    assert kafka["rows"] == [{"key": "count", "label": "count", "value": 3}]
    # services reads the children of the fleet status, never the servers.
    services = await _read("services")
    assert services["rows"] == [{"name": "c1", "state": "up"}]
    for unknown in ("nope", "writes_only"):
        with pytest.raises(OperationRefused) as refused:
            await _read(unknown)
        assert refused.value.code == "NOT_FOUND"


async def test_down_connector_gives_degraded_cached_rows(fleet: Fleet) -> None:
    healthy = await _read("arr_mcp")
    fleet.down.add("arr-mcp")
    degraded = await _read("arr_mcp")
    assert degraded["degraded"] is True and degraded["rows"] == healthy["rows"]
    # Another tenant never sees this tenant's cached rows.
    other = await _read("arr_mcp", "tenant:two")
    assert other["degraded"] is True and other["rows"] == []


def test_the_operation_is_registered_under_the_fleet_read_scope() -> None:
    (op,) = ecosystem.operations()
    assert op.id == "ecosystem.widgets.read" and op.scopes == frozenset({"fleet:read"})
    assert ecosystem.widget_name("paperless-ngx-mcp") == "paperless_ngx_mcp"


def test_the_gateway_widget_registry_is_no_longer_read() -> None:
    from pathlib import Path

    source = Path(ecosystem.__file__).read_text()
    assert "arbora.gateway" not in source and "get_registry" not in source
