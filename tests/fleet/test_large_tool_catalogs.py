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


# ── R072: the engine catalog carries each tool's contract ────────────────────


def _engine_snapshot(*, hints: dict[str, bool] | None) -> SimpleNamespace:
    import json

    body = json.dumps(
        {
            "name": "photocraft_cmd_info",
            "description": "read one document",
            "input_schema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            "output_schema": None,
        }
    ).encode()
    entry = SimpleNamespace(
        kind="tool",
        server_name="craft",
        upstream_name="photocraft_cmd_info",
        summary="read one document",
        tool_hints=hints,
    )
    provided = SimpleNamespace(entry=entry, content=SimpleNamespace(body=body))
    return SimpleNamespace(servers=[SimpleNamespace(provides=[provided])])


@pytest.mark.spec("ARBORA-FLEET-R072")
def test_engine_tool_facts_project_to_mcp_hints() -> None:
    from epistemic_graph.generated._shared import AgentComponentFactsTool, ToolEffect

    from arbora.fleet.epistemic_adapter import _tool_hints

    facts = AgentComponentFactsTool(
        facts="tool",
        effect=ToolEffect.READ,
        required_scopes=[],
        read_only_hint=True,
        destructive_hint=False,
    )
    assert _tool_hints(facts) == {"readOnlyHint": True, "destructiveHint": False}
    assert _tool_hints(None) is None


@pytest.mark.spec("ARBORA-FLEET-R072")
def test_an_engine_tool_item_has_its_schema_and_read_effect_without_a_probe() -> None:
    from arbora.api.registry import Effect
    from arbora.fleet.catalog_items import items_from_eg_catalog
    from arbora.fleet.gateway_ops import annotation_effect

    (item,) = items_from_eg_catalog(_engine_snapshot(hints={"readOnlyHint": True}))
    assert item.schema["required"] == ["id"]
    assert item.schema_fingerprint is not None
    assert annotation_effect(item.annotations)[0] == Effect.READ


@pytest.mark.spec("ARBORA-FLEET-R072")
def test_a_probe_without_hints_keeps_the_engine_contract() -> None:
    from arbora.fleet.catalog_items import CatalogItem, items_from_eg_catalog
    from arbora.fleet.catalog_sources import _merge_probed_item, _ServerPolicy

    (engine,) = items_from_eg_catalog(_engine_snapshot(hints={"readOnlyHint": True}))
    merged = {engine.id: engine}
    probed = CatalogItem(
        id=engine.id, kind="tool", name=engine.name, server="craft", schema={}
    )
    policy = _ServerPolicy(frozenset({"mcp:delegate"}), frozenset(), "delegated", {})
    _merge_probed_item(merged, probed, policy=policy, subject_id=None)
    assert merged[engine.id].schema == engine.schema
    assert merged[engine.id].annotations == {"readOnlyHint": True}


# ── R068.1: every list family pages ──────────────────────────────────────────


class _PagedPrompts:
    def __init__(self, count: int, page: int) -> None:
        self._count, self._page = count, page

    async def list_prompts(self, *, params: Any = None) -> SimpleNamespace:
        start = 0 if params is None else int(params.cursor)
        end = min(start + self._page, self._count)
        prompts = [SimpleNamespace(name=f"p{i}") for i in range(start, end)]
        return SimpleNamespace(
            prompts=prompts, next_cursor=str(end) if end < self._count else None
        )


@pytest.mark.spec("ARBORA-FLEET-R068.1")
def test_prompts_resources_and_templates_page_like_tools() -> None:
    from arbora.fleet.multiplexer import _list_all

    prompts = asyncio.run(_list_all(_PagedPrompts(25, 10), "list_prompts", "prompts"))
    assert [p.name for p in prompts] == [f"p{i}" for i in range(25)]


# ── R070.1: a refresh drops what the server stopped serving ──────────────────


def _merge(info: dict[str, Any], held: list[CatalogItem]) -> dict[str, CatalogItem]:
    from arbora.fleet.catalog_sources import _merge_probed_items, _ServerPolicy

    merged = {item.id: item for item in held}
    policy = _ServerPolicy(frozenset({"mcp:delegate"}), frozenset(), "delegated", {})
    _merge_probed_items(merged, {"arr": policy}, {"arr": None}, {"arr": info})
    return merged


def _held(kind: Any, name: str) -> CatalogItem:
    return CatalogItem(
        id=f"fleet:{kind}:arr/{name}", kind=kind, name=name, server="arr"
    )


@pytest.mark.spec("ARBORA-FLEET-R070.1")
def test_a_refresh_drops_tools_prompts_and_skills_the_server_no_longer_serves() -> None:
    held = [
        _held("tool", "kept"),
        _held("tool", "removed"),
        _held("prompt", "old_prompt"),
        _held("skill", "old_skill"),
        _held("resource", "arr-contracts"),
    ]
    info = {
        "error": None,
        "tools": [{"name": "kept"}, {"name": "bazarr_get_system_status"}],
        "native_prompts": [{"name": "new_prompt"}],
        "skills": [{"name": "new_skill"}],
        "resources": [],
    }
    merged = _merge(info, held)
    assert sorted(merged) == [
        "fleet:prompt:arr/new_prompt",
        "fleet:resource:arr/arr-contracts",
        "fleet:skill:arr/new_skill",
        "fleet:tool:arr/bazarr_get_system_status",
        "fleet:tool:arr/kept",
    ]


@pytest.mark.spec("ARBORA-FLEET-R070.1")
def test_a_failed_family_or_probe_drops_nothing() -> None:
    held = [_held("tool", "removed"), _held("prompt", "old_prompt")]
    failed_family: dict[str, Any] = {
        "error": None,
        "tools": [],
        "native_prompts": [],
        "catalog_family_errors": {"prompts": "FleetCatalogPageError"},
    }
    assert "fleet:prompt:arr/old_prompt" in _merge(failed_family, held)
    assert "fleet:tool:arr/removed" not in _merge(failed_family, held)
    failed = {"error": "child down", "tools": []}
    assert sorted(_merge(failed, held)) == sorted(item.id for item in held)


# ── R074: find ranks large catalogs by discriminating terms ──────────────────


def _arr_like_catalog() -> list[tuple[str, str]]:
    apps = ["radarr", "sonarr", "lidarr", "readarr", "prowlarr", "bazarr"]
    verbs = ["list", "get", "update", "delete"]
    objects = [f"thing{i}" for i in range(60)]
    rows = [
        (f"{app}_{verb}_{obj}", f"{verb.capitalize()} {obj} in {app}")
        for app in apps
        for verb in verbs
        for obj in objects
    ]
    # The arr queue tools describe themselves with the request's common words.
    rows += [
        (f"{app}_list_queue", f"List pending media in the {app} download queue")
        for app in apps
    ]
    rows += [
        ("seerr_get_requests", "Get media requests, filtered by status"),
        ("seerr_get_request_count", "Count requests by status"),
        ("seerr_get_user", "Get one user"),
    ]
    return rows


@pytest.mark.spec("ARBORA-FLEET-R074")
def test_find_ranks_the_named_system_and_object_first_in_a_large_catalog() -> None:
    from arbora.fleet.discovery_ranking import LexicalRanker
    from arbora.fleet.multiplexer import MCPMultiplexer

    catalog = _arr_like_catalog()
    query = "list pending media requests in seerr"
    ranker = LexicalRanker(query, (f"{n} {d}" for n, d in catalog))
    ranked = sorted(catalog, key=lambda row: -ranker.score(*row))
    assert [name for name, _ in ranked[:2]] == [
        "seerr_get_requests",
        "seerr_get_request_count",
    ]
    # The plain overlap ratio ties the seerr tools with generic "list media" rows.
    plain = sorted(
        catalog, key=lambda row: -MCPMultiplexer._relevance(query, f"{row[0]} {row[1]}")
    )
    assert plain[0][0] != "seerr_get_requests"


# ── R075: a slow tool's call deadline is not the connect budget ──────────────


@pytest.mark.spec("ARBORA-FLEET-R075")
def test_the_call_deadline_ignores_the_connect_budget() -> None:
    from arbora.fleet.child_resilience import ChildRuntime
    from arbora.fleet.multiplexer import _child_call_timeout

    assert _child_call_timeout({"timeout": 15}, None) == 300.0
    assert _child_call_timeout({"timeout": 15, "call_timeout": 120}, None) == 120.0
    assert _child_call_timeout({"call_timeout": 120}, 5.0) == 5.0
    assert ChildRuntime("slow", {"timeout": 15}).call_timeout == 300.0


@pytest.mark.spec("ARBORA-FLEET-R075")
def test_a_call_slower_than_the_connect_budget_completes(monkeypatch) -> None:
    from arbora.fleet import multiplexer as mux_module

    mux = mux_module.MCPMultiplexer.__new__(mux_module.MCPMultiplexer)
    mux._catalog_epoch = 0
    monkeypatch.setattr(mux_module, "_require_fleet_capability", lambda *a, **k: None)
    monkeypatch.setattr(
        type(mux), "load_catalog", lambda self: {"egeria": {"timeout": 0.05}}
    )

    async def slow(*_args: Any) -> str:
        await asyncio.sleep(0.2)
        return "200 glossary terms"

    monkeypatch.setattr(mux, "_call_ephemeral_child_session", slow)
    result = asyncio.run(mux._call_service_child_once("egeria", "t", {}, timeout=None))
    assert result == "200 glossary terms"


@pytest.mark.spec("ARBORA-FLEET-R075")
def test_the_operator_call_timeout_reaches_the_child(monkeypatch, tmp_path) -> None:
    import json

    from arbora.fleet import multiplexer as mux_module

    config = tmp_path / "mcp_config.json"
    config.write_text(
        json.dumps({"mcpServers": {"egeria-mcp": {"url": "x", "call_timeout": 240}}})
    )
    monkeypatch.setattr("arbora.core.settings.mcp_config_path", lambda: config)
    assert mux_module._operator_call_timeouts() == {"egeria-mcp": 240}
    config.write_text("{not json")
    assert mux_module._operator_call_timeouts() == {}
