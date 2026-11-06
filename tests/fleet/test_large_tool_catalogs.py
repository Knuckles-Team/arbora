"""Connectors with very large tool catalogs (ARBORA-FLEET-R068..R071).

A connector that serves one tool per upstream operation lists thousands of
tools. Arbora follows its ``tools/list`` pages to the end, admits the whole
catalog up to a configured ceiling, keeps it current without a restart, and
authorizes ``fleet.call`` against the current catalog.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP

from arbora.fleet.catalog_composition import probe_primer
from arbora.fleet.catalog_items import CatalogItem
from arbora.fleet.multiplexer import (
    FleetCatalogPageError,
    _bounded_tool_catalog,
    _list_all_tools,
)
from arbora.fleet.multiplexer_ops import MultiplexerOps

# ── R068: tools/list pagination ──────────────────────────────────────────────


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description="",
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        output_schema=None,
        annotations=None,
        meta=None,
    )


class _PagedSession:
    """A child session that serves its catalog in fixed pages."""

    def __init__(
        self,
        names: list[str],
        page: int,
        *,
        loop_at: int | None = None,
        fail_at: str | None = None,
    ):
        self._names = names
        self._page = page
        self._loop_at = loop_at
        self._fail_at = fail_at
        self.requests: list[str | None] = []

    async def list_tools(self, *, params: Any = None) -> SimpleNamespace:
        cursor = None if params is None else params.cursor
        self.requests.append(cursor)
        if cursor is not None and cursor == self._fail_at:
            raise ConnectionError("page fetch failed")
        start = 0 if cursor is None else int(cursor)
        end = start + self._page
        tools = [_tool(name) for name in self._names[start:end]]
        if self._loop_at is not None and end >= self._loop_at:
            return SimpleNamespace(tools=tools, next_cursor=str(self._loop_at))
        more = end < len(self._names)
        return SimpleNamespace(tools=tools, next_cursor=str(end) if more else None)


@pytest.mark.spec("ARBORA-FLEET-R068")
def test_tools_list_is_followed_to_its_last_page() -> None:
    names = [f"op_{index}" for index in range(2_500)]
    session = _PagedSession(names, page=1_000)
    tools = asyncio.run(_list_all_tools(session))
    assert [tool.name for tool in tools] == names
    assert session.requests == [None, "1000", "2000"]


@pytest.mark.spec("ARBORA-FLEET-R068")
def test_a_repeated_cursor_refuses_the_listing_whole() -> None:
    session = _PagedSession([f"op_{i}" for i in range(30)], page=10, loop_at=10)
    with pytest.raises(FleetCatalogPageError, match="cursor repeated"):
        asyncio.run(_list_all_tools(session))


@pytest.mark.spec("ARBORA-FLEET-R068")
def test_a_failed_page_refuses_the_listing_typed_never_partial() -> None:
    session = _PagedSession([f"op_{i}" for i in range(30)], page=10, fail_at="20")
    with pytest.raises(FleetCatalogPageError, match="ConnectionError"):
        asyncio.run(_list_all_tools(session))
    assert session.requests == [None, "10", "20"]


@pytest.mark.spec("ARBORA-FLEET-R068")
def test_a_real_paginating_server_is_read_over_the_wire() -> None:
    """A FastMCP server with a page size serves ``nextCursor``; all pages arrive."""
    server = FastMCP("paged", list_page_size=100)
    for index in range(450):
        server.tool(name=f"op_{index}")(lambda q="": q)

    async def drive() -> list[Any]:
        async with Client(server) as client:
            first = await client.session.list_tools()
            assert first.next_cursor is not None
            assert len(first.tools) == 100
            return await _list_all_tools(client.session)

    tools = asyncio.run(drive())
    assert sorted(tool.name for tool in tools) == sorted(
        f"op_{index}" for index in range(450)
    )
    assert len(_bounded_tool_catalog(tools)) == 450


# ── R069: no per-connector tool cap ──────────────────────────────────────────


@pytest.mark.spec("ARBORA-FLEET-R069")
def test_a_catalog_past_the_old_fixed_cap_is_admitted_whole() -> None:
    tools = [_tool(f"op_{index}") for index in range(9_011)]
    assert len(_bounded_tool_catalog(tools)) == 9_011


@pytest.mark.spec("ARBORA-FLEET-R069")
def test_a_paged_catalog_of_twenty_thousand_tools_is_admitted_whole() -> None:
    names = [f"op_{index}" for index in range(20_000)]
    session = _PagedSession(names, page=500)
    tools = asyncio.run(_list_all_tools(session))
    assert len(session.requests) == 40
    assert [entry["name"] for entry in _bounded_tool_catalog(tools)] == names


# ── R070: refresh without a restart ──────────────────────────────────────────


class _Mux:
    """A probe cache whose entries go stale on demand."""

    def __init__(self) -> None:
        self._probe_cache: dict[str, dict[str, Any]] = {}
        self.stale: set[str] = set()
        self.calls: list[tuple[str, bool]] = []
        self.tools = ["a"]
        self.fail = False

    def _probe_cache_hit(self, server: str) -> dict[str, Any] | None:
        return None if server in self.stale else self._probe_cache.get(server)

    async def probe_server(self, server: str, force: bool = False) -> dict:
        self.calls.append((server, force))
        info: dict[str, Any]
        if self.fail:
            info = {"tools": [], "error": "child down"}
        else:
            info = {"tools": [{"name": n} for n in self.tools], "error": None}
            self.stale.discard(server)
        info["probed_at"] = float(len(self.calls))
        self._probe_cache[server] = info
        return info


@pytest.mark.spec("ARBORA-FLEET-R070")
def test_a_stale_probe_is_served_while_one_background_reprobe_runs() -> None:
    mux = _Mux()
    prime = probe_primer(mux, retry_s=0.0)

    async def drive() -> None:
        await prime(["craft"])
        mux.stale.add("craft")
        mux.tools = ["a", "b"]
        await prime(["craft"])
        await prime(["craft"])
        # The stale entry answered at once; one re-probe is in flight.
        assert [t["name"] for t in mux._probe_cache["craft"]["tools"]] == ["a"]
        await asyncio.gather(*prime._background.values())

    asyncio.run(drive())
    assert mux.calls == [("craft", False), ("craft", False)]
    assert [t["name"] for t in mux._probe_cache["craft"]["tools"]] == ["a", "b"]


@pytest.mark.spec("ARBORA-FLEET-R070")
def test_a_failed_reprobe_keeps_the_last_good_catalog() -> None:
    mux = _Mux()
    prime = probe_primer(mux, retry_s=0.0)
    asyncio.run(prime(["craft"]))
    mux.fail = True
    outcome = asyncio.run(prime.refresh(["craft"]))
    assert mux._probe_cache["craft"]["error"] is None
    assert outcome["craft"]["tools"] == 1


@pytest.mark.spec("ARBORA-FLEET-R070")
def test_the_explicit_refresh_reprobes_now_and_is_throttled() -> None:
    now = [0.0]
    mux = _Mux()
    prime = probe_primer(mux, retry_s=60.0, clock=lambda: now[0])
    ops = _ops(refresh=prime.refresh)
    caller = SimpleNamespace(effective_scopes=frozenset({"fleet:control"}))
    first = asyncio.run(ops.refresh_catalog(caller, servers=["craft"]))
    mux.tools = ["a", "b", "c"]
    second = asyncio.run(ops.refresh_catalog(caller, servers=["craft"]))
    now[0] = 61.0
    third = asyncio.run(ops.refresh_catalog(caller, servers=["craft"]))
    assert mux.calls == [("craft", True), ("craft", True)]
    assert [r["servers"]["craft"]["tools"] for r in (first, second, third)] == [
        1,
        1,
        3,
    ]


@pytest.mark.spec("ARBORA-FLEET-R070")
@pytest.mark.parametrize("servers", [(), tuple(f"s{i}" for i in range(33))])
def test_the_explicit_refresh_refuses_a_bad_server_list(servers) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    ops = _ops(refresh=probe_primer(_Mux()).refresh)
    with pytest.raises(OperationRefused) as caught:
        asyncio.run(ops.refresh_catalog(SimpleNamespace(), servers=servers))
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-FLEET-R070")
def test_the_refresh_op_is_a_control_scoped_manage_op() -> None:
    from arbora.api.ops import fleet
    from arbora.api.registry import Registry, Verb

    spec = Registry(fleet.specs())["fleet.catalog.refresh"]
    assert spec.verb == Verb.MANAGE
    assert spec.scopes == frozenset({"fleet:control"})


# ── R071: fleet.call authority is the current catalog ────────────────────────


def _ops(**kwargs: Any) -> MultiplexerOps:
    return MultiplexerOps(
        catalog=kwargs.pop("catalog", None),
        sessions=SimpleNamespace(),  # type: ignore[arg-type]
        loadable=None,  # type: ignore[arg-type]
        mount=None,  # type: ignore[arg-type]
        notify=None,  # type: ignore[arg-type]
        invoke=None,  # type: ignore[arg-type]
        health=dict,
        callable_item=_allow,
        **kwargs,
    )


async def _allow(_item: Any, _caller: Any) -> bool:
    return True


class _Catalog:
    """The admitted catalog: the cached probe's tools of one server."""

    def __init__(self, mux: _Mux) -> None:
        self._mux = mux

    async def _raw_get(self, item_id: str) -> CatalogItem | None:
        info = self._mux._probe_cache.get("craft", {})
        for row in info.get("tools", ()):
            if item_id == f"fleet:tool:craft/{row['name']}":
                return CatalogItem(
                    id=item_id, kind="tool", name=row["name"], server="craft"
                )
        return None


@pytest.mark.spec("ARBORA-FLEET-R071")
def test_a_tool_new_since_the_cached_probe_is_callable_without_restart() -> None:
    mux = _Mux()
    prime = probe_primer(mux, retry_s=60.0)
    ops = _ops(catalog=_Catalog(mux), prime=prime, refresh=prime.refresh)
    caller = SimpleNamespace(effective_scopes=frozenset({"mcp:delegate"}))
    asyncio.run(ops.admitted_tool(caller, "craft", "a"))
    mux.tools = ["a", "new_op"]
    item = asyncio.run(ops.admitted_tool(caller, "craft", "new_op"))
    assert item.name == "new_op"
    assert mux.calls == [("craft", False), ("craft", True)]


@pytest.mark.spec("ARBORA-FLEET-R071")
def test_an_unknown_tool_is_refused_after_one_throttled_refresh() -> None:
    mux = _Mux()
    prime = probe_primer(mux, retry_s=60.0)
    ops = _ops(catalog=_Catalog(mux), prime=prime, refresh=prime.refresh)
    caller = SimpleNamespace(effective_scopes=frozenset({"mcp:delegate"}))
    for _ in range(3):
        with pytest.raises(PermissionError, match="fleet tool is unavailable"):
            asyncio.run(ops.admitted_tool(caller, "craft", "missing"))
    assert mux.calls == [("craft", False), ("craft", True)]
