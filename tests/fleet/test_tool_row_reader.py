"""Every tool-row site reads through one ``ToolRowReader`` (ARBORA-FLEET-R073.2)."""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from arbora.fleet.multiplexer import MCPMultiplexer
from arbora.fleet.tool_rows import ProbeToolRows, ToolRowReader


class _Recording:
    """A reader whose rows are NOT the probe's: a site that bypasses it shows."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    async def rows(self, server: str, info: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.reads.append(server)
        return [{"name": "from_reader", "description": "d", "inputSchema": {}}]

    async def count(self, server: str, info: Mapping[str, Any]) -> int:
        self.reads.append(server)
        return 1


def _mux(reader: ToolRowReader) -> Any:
    return SimpleNamespace(
        _tool_rows=reader,
        _tool_enabled=lambda server, tool: True,
        tool_dispatchable=lambda name: True,
        server_prefix=lambda server: "p",
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
    from arbora.fleet import multiplexer as module

    monkeypatch.setattr(module, "require_fleet_capability", lambda *a, **k: None)
    reader = _Recording()
    mux = _mux(reader)

    async def probe(name: str) -> dict[str, Any]:
        return {"tools": [{"name": "probe_only"}], "error": None}

    mux.probe_server = probe
    rows = await MCPMultiplexer.delegated_server_tools(mux, "srv")
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
