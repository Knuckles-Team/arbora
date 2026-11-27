"""Every tool-row site reads through one ``ToolRowReader`` (ARBORA-FLEET-R073.2)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.fleet.multiplexer import MCPMultiplexer
from arbora.fleet.tool_rows import EngineToolRows, ProbeToolRows, ToolRowReader


class _Recording:
    """A reader whose rows are NOT the probe's: a site that bypasses it shows."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    async def rows(
        self, server: str, info: Mapping[str, Any], *, schemas: bool = False
    ) -> list[dict[str, Any]]:
        self.reads.append(server)
        return [{"name": "from_reader", "description": "d", "inputSchema": {}}]

    def resident(self, info: dict[str, Any]) -> dict[str, Any]:
        return info

    async def count(self, server: str, info: Mapping[str, Any]) -> int:
        self.reads.append(server)
        return 1


def _mux(reader: ToolRowReader) -> Any:
    return SimpleNamespace(
        _tool_rows=reader,
        _fleet=SimpleNamespace(config=RuntimeConfig()),
        mounts=SimpleNamespace(
            _tool_enabled=lambda server, tool: True, server_prefix=lambda server: "p"
        ),
        tool_dispatchable=lambda name: True,
    )


async def test_the_probe_reader_copies_rows_and_counts_them() -> None:
    info = {"tools": [{"name": "a"}, {"name": "b"}, "not-a-row"]}
    reader = ProbeToolRows()
    rows = await reader.rows("s", info)
    assert [row["name"] for row in rows] == ["a", "b"]
    rows[0]["name"] = "changed"
    assert info["tools"][0]["name"] == "a"
    assert await reader.count("s", {"tools": [{"name": "a"}]}) == 1
    assert await reader.count("s", {}) == 0
    assert isinstance(reader, ToolRowReader)


async def test_the_catalog_detail_reads_through_the_reader() -> None:
    reader = _Recording()
    rows = await MCPMultiplexer._catalog_detail_tools(
        _mux(reader), "srv", "p", {"tools": [{"name": "probe_only"}]}
    )
    assert [row["tool"] for row in rows] == ["from_reader"]
    assert reader.reads == ["srv"]


async def test_the_fleet_listing_reads_through_the_reader() -> None:
    reader = _Recording()
    mux = _mux(reader)
    mux._catalog_tool_partition = lambda *a: MCPMultiplexer._catalog_tool_partition(
        mux, *a
    )
    mux.children = {}
    mux._stamp_catalog_freshness = lambda *a: None
    entry = await MCPMultiplexer._catalog_fleet_entry(
        mux, "srv", {"tools": [{"name": "probe_only"}]}, True, 0.0, 1.0, True
    )
    assert entry["tool_count"] == 1
    assert entry["tools"] == ["p_from_reader"] or "from_reader" in entry["tools"][0]
    assert reader.reads == ["srv"]


async def test_the_delegated_server_tools_read_through_the_reader(monkeypatch) -> None:
    from arbora.fleet import child_mounts as module

    monkeypatch.setattr(module, "require_fleet_capability", lambda *a, **k: None)
    from arbora.fleet.child_mounts import ChildMounts

    reader = _Recording()

    async def probe(name: str) -> dict[str, Any]:
        return {"tools": [{"name": "probe_only"}], "error": None}

    mounts: Any = SimpleNamespace(
        _tool_rows=lambda: reader,
        _fleet=SimpleNamespace(config=RuntimeConfig()),
        probe_server=probe,
    )
    rows = await ChildMounts.delegated_server_tools(mounts, "srv")
    assert [row["name"] for row in rows] == ["from_reader"]


async def test_the_discovery_rows_read_through_the_reader() -> None:
    reader = _Recording()
    mux = _mux(reader)
    mux._probe_age_and_staleness = lambda info, now, ttl: (0.0, False)
    rows = await MCPMultiplexer._discovery_rows(
        mux,
        "srv",
        {"tools": [{"name": "probe_only"}], "skills": []},
        {"srv": {}},
        set(),
        0.0,
        1.0,
    )
    assert [row["tool"] for row in rows if row["kind"] == "tool"] == ["from_reader"]


# ── R073.2: the engine reader, the pin-lag choice and the flat probe cache ────


class _Port:
    """A structural double of the catalog port's component search and content reads."""

    def __init__(self, tools: dict[str, list[str]], page: int = 2) -> None:
        self.tools = tools
        self.page = page
        self.requests: list[Any] = []
        self.content_reads: list[str] = []

    async def search_components(self, request: Any) -> Any:
        self.requests.append(request)
        names = self.tools.get(request.server_name, [])
        start = int(request.cursor or 0)
        chunk = names[start : start + self.page]
        more = start + self.page < len(names)
        entries = tuple(
            SimpleNamespace(
                component_id=f"c:{request.server_name}/{n}",
                upstream_name=n,
                summary=f"does {n}",
                entry_revision=1,
            )
            for n in chunk
        )
        return SimpleNamespace(
            entries=entries, next_cursor=str(start + self.page) if more else None
        )

    async def component_content(
        self, *, tenant_id: str, component_id: str, **_: Any
    ) -> Any:
        self.content_reads.append(component_id)
        return SimpleNamespace(body=b'{"input_schema": {"type": "object"}}')


def _engine(port: _Port) -> EngineToolRows:
    return EngineToolRows(port, tenant_id="t", scope=contextlib.nullcontext)


@pytest.mark.spec("ARBORA-FLEET-R073.2")
async def test_the_engine_reader_pages_one_server_by_name_and_reads_schemas_on_demand() -> (
    None
):
    port = _Port({"alpha": ["a", "b", "c"], "beta": ["z"]})
    reader = _engine(port)
    rows = await reader.rows("alpha", {"tools": [{"name": "probe_only"}]})
    assert [r["name"] for r in rows] == ["a", "b", "c"]
    assert all("inputSchema" not in r for r in rows) and port.content_reads == []
    assert {r.server_name for r in port.requests} == {"alpha"}
    assert [r.kinds for r in port.requests] == [("tool",), ("tool",)]
    assert await reader.count("beta", {}) == 1
    detailed = await reader.rows("beta", {}, schemas=True)
    assert detailed[0]["inputSchema"] == {"type": "object"}
    assert port.content_reads == ["c:beta/z"]


@pytest.mark.spec("ARBORA-FLEET-R073.2")
def test_the_pin_lag_guard_picks_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    from arbora.fleet import tool_rows
    from arbora.pin_lag import medulla

    def pick() -> Any:
        return tool_rows.tool_row_reader(
            _Port({}), tenant_id="t", scope=contextlib.nullcontext
        )

    monkeypatch.setattr(medulla, "component_search_filters_served", lambda: False)
    assert isinstance(pick(), ProbeToolRows)
    monkeypatch.setattr(medulla, "component_search_filters_served", lambda: True)
    assert isinstance(pick(), EngineToolRows)
    assert isinstance(pick(), ToolRowReader)


@pytest.mark.spec("ARBORA-FLEET-R073.2")
def test_probe_memory_is_flat_in_catalog_size() -> None:
    reader = _engine(_Port({}))

    def resident(count: int) -> dict[str, Any]:
        info = {
            "tools": [{"name": f"t{i}", "description": "d" * 40} for i in range(count)]
        }
        slim = reader.resident(info)
        return {k: v for k, v in slim.items() if k != "tool_count"} | {
            "n": slim["tool_count"]
        }

    small, large = resident(1_000), resident(50_000)
    assert small["tools"] == [] and large["tools"] == []
    assert (small["n"], large["n"]) == (1_000, 50_000)
    assert len(small["tools_digest"]) == len(large["tools_digest"]) == 64
    assert small["tools_digest"] != large["tools_digest"]
    assert len(json.dumps({**small, "n": 0})) == len(json.dumps({**large, "n": 0}))


@pytest.mark.spec("ARBORA-FLEET-R073.2")
def test_the_multiplexer_probe_cache_keeps_no_rows_once_the_engine_reader_is_bound() -> (
    None
):
    mux: Any = SimpleNamespace(
        _tool_rows=_engine(_Port({})),
        _probe_cache={},
        _drop_discovery_bindings_for_server=lambda name: None,
        load_catalog=lambda: {},
    )
    info = {"tools": [{"name": "a"}, {"name": "b"}], "error": None}
    returned = MCPMultiplexer._cache_probe(mux, "srv", info)
    assert returned is mux._probe_cache["srv"]
    assert returned["tools"] == [] and returned["tool_count"] == 2
    assert "probed_at" in returned
