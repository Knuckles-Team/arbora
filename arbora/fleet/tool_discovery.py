"""Tool discovery: ranking, discovery and catalog listing over the probed fleet (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import asyncio
import logging
import time
import typing as _typing

if _typing.TYPE_CHECKING:
    from mcp_types import Tool as MCPTool
import arbora.core.priority as _resource_priority
from arbora.core.capability_contract import Capability
from arbora.fleet.child_mounts import (
    clean_tool_name,
)
from arbora.fleet.fleet_probe import FleetProbe
from arbora.fleet.local_skill_catalog import (
    LOCAL_SKILLS_SERVER,
)

logger = logging.getLogger("mcp_multiplexer")


def _tool_is_verbose(tool: MCPTool) -> bool:
    """Whether a child tool is tagged ``verbose`` (FastMCP propagates tags in
    ``_meta``). Verbose 1:1 tools (e.g. arbora's granular per-action surface)
    are kept in the catalog but NOT auto-exposed by an always-on child — they
    load on demand via ``find_tools``/``load_tools`` to conserve context
    (CONCEPT:MERISTEM-ECO.multiplexer.condensed-server-load)."""
    meta = getattr(tool, "meta", None)
    if not isinstance(meta, dict):
        return False
    tags = (meta.get("fastmcp") or {}).get("tags") or []
    return "verbose" in tags


class ToolDiscovery:
    """Ranks and lists the fleet's tools and skills for a caller.

    Built from the shared state and the calls it needs, with no reference to the
    multiplexer that owns that state."""

    def __init__(
        self,
        *,
        fleet: _typing.Any,
        children: dict[str, _typing.Any],
        exposed: set[str],
        probe_cache: dict[str, dict],
        probe: FleetProbe,
        mounts: _typing.Callable[[], _typing.Any],
        tool_to_server: _typing.Callable[[], dict[str, tuple[str, str]]],
        tool_rows: _typing.Callable[[], _typing.Any],
        load_catalog: _typing.Callable[..., _typing.Any],
        tool_dispatchable: _typing.Callable[..., _typing.Any],
    ) -> None:
        self._fleet = fleet
        self.children = children
        self._exposed = exposed
        self._probe_cache = probe_cache
        self._probe = probe
        self._mounts = mounts
        self._tool_to_server = tool_to_server
        self._tool_rows = tool_rows
        self._load_catalog = load_catalog
        self._tool_dispatchable = tool_dispatchable

    @staticmethod
    def _fleet_skills_without_local_duplicates(
        server: str, info: dict, local_names: set[str]
    ) -> list[dict] | None:
        """One server's skills with a local-duplicate name dropped, logged.

        Returns ``None`` when nothing collided, so the caller can keep the
        original entry untouched rather than rebuild an identical one.
        """
        skills = info.get("skills") or []
        kept = [s for s in skills if s.get("name") not in local_names]
        if len(kept) == len(skills):
            return None
        for dropped in skills:
            if dropped.get("name") in local_names:
                logger.info(
                    "Fleet-harvested skill %r from %r shadowed by a local "
                    "skill of the same name",
                    dropped.get("name"),
                    server,
                )
        return kept

    def _dedup_fleet_skills_against_local(
        self, probe: dict, local_names: set[str]
    ) -> dict:
        """Drop a fleet-harvested skill whose name collides with a local one.

        A local skill (served from a package installed in THIS process) takes
        precedence over the same name surfacing from a live-probed child: it
        is this process's own authoritative resolution and costs no child
        connection to read. Never mutates a cached probe entry in place — a
        filtered copy is returned only for a server whose skills actually
        collide, so ``self._probe_cache`` stays exactly what was harvested.
        """
        if not local_names:
            return probe
        deduped = dict(probe)
        for server, info in probe.items():
            if server == LOCAL_SKILLS_SERVER:
                continue
            kept = self._fleet_skills_without_local_duplicates(
                server, info, local_names
            )
            if kept is not None:
                deduped[server] = {**info, "skills": kept}
        return deduped

    def _results_or_fallback(self, ranked: list[dict], probe: dict) -> list[dict]:
        """The ranked rows, or every mountable server when nothing matched.

        Nothing matched but a reachable server exists, so the caller can still load
        by server; when every server errored the results stay empty and
        ``unavailable`` tells the story.
        """
        if ranked or all(info.get("error") for info in probe.values()):
            return ranked
        return self._probe._server_level_fallback()

    async def _probe_rows(
        self, probe: dict, catalog: dict[str, dict], loaded: set[str]
    ) -> tuple[list[dict], dict[str, str]]:
        """Every loadable tool and skill row of the probe, and the servers that erred.

        One ranked capability space (CONCEPT:MERISTEM-KG.retrieval.unified-capability-contract):
        fleet tools AND fleet-served skill:// resources are rows of one list, each
        carrying a ``bind`` dict, the exact kwargs `graph_orchestrate` needs to run it.
        ARBORA-FLEET-R031: a server whose live probe errored (a timeout is the common
        case) is not thereby invisible: its already-admitted catalog snapshot (never a
        probe) is the fallback source, honestly marked stale.
        """
        now, ttl = time.time(), self._probe._probe_ttl()
        fallbacks = self._probe._catalog_tool_probe_info()
        rows: list[dict] = []
        unavailable: dict[str, str] = {}
        for server, info in probe.items():
            if info.get("error"):
                unavailable[server] = info["error"]
                info = fallbacks.get(server)
            if info:
                rows.extend(
                    await self._discovery_rows(server, info, catalog, loaded, now, ttl)
                )
        return rows, unavailable

    async def _discovery_rows(
        self,
        server: str,
        info: dict,
        catalog: dict[str, dict],
        loaded: set[str],
        now: float,
        ttl: float,
    ) -> list[dict]:
        """Every loadable tool AND skill row of one probed server, unordered.

        Truthful freshness for every surfaced tool/skill (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog):
        a row from a probe that ran seconds or minutes ago carries its real age,
        and ``is_stale`` is computed from that age against the TTL
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness). find_tools
        surfaces only loadable (enabled) tools; disabled-but-capable tools stay
        visible through list_catalog.
        """
        probe_age, is_stale = self._probe._probe_age_and_staleness(info, now, ttl)
        common = {
            "mountable": server in catalog,
            "age_s": probe_age,
            "stale": is_stale,
        }
        rows: list[dict] = []
        for entry in await self._tool_rows().rows(server, info):
            tool = entry["name"]
            if not self._mounts()._tool_enabled(server, tool):
                continue
            desc = entry.get("description", "")
            prefixed = clean_tool_name(
                self._mounts().server_prefix(server), server, tool
            )
            capability = Capability(
                kind="tool",
                id=f"tool_{server}_{tool}",
                name=tool,
                description=desc,
                score=0.0,
                server=server,
                source="fleet_probe",
            )
            rows.append(
                {
                    "kind": "tool",
                    "server": server,
                    "tool": tool,
                    "prefixed_name": prefixed,
                    "description": desc,
                    "mounted": prefixed in loaded,
                    "bind": capability.to_binding(),
                    **common,
                }
            )
        for entry in info.get("skills", []) or []:
            skill = entry.get("name")
            if not skill:
                continue
            desc = entry.get("description", "")
            capability = Capability(
                kind="skill",
                id=f"skill_{server}_{skill}",
                name=skill,
                description=desc,
                score=0.0,
                server=server,
                source="fleet_probe",
            )
            rows.append(
                {
                    "kind": "skill",
                    "server": server,
                    "skill": skill,
                    "uri": entry.get("uri", ""),
                    "description": desc,
                    "mounted": False,
                    "bind": capability.to_binding(),
                    **common,
                }
            )
        return rows

    async def _ordered_discovery_rows(
        self, query: str, rows: list[dict], top_k: int
    ) -> list[dict]:
        """The rows in the ranking port's order, the first ``top_k`` of them.

        ARBORA-FLEET-R083.3: a ranking is medulla's choice; the lexical floor answers
        when the engine cannot. The floor drops a row no routing term matches, as the
        retired scorer did; an engine order is kept whole, since the engine owns the
        semantic match. Each row's ``score`` is the floor's evidence for it.
        """
        from arbora.api.domain.fleet.lexical_floor import evidence
        from arbora.api.domain.fleet.ranking import RankCandidate

        candidates = [
            RankCandidate(
                f"{row['kind']}:{row['server']}:{row.get('tool') or row.get('skill')}",
                str(row.get("tool") or row.get("skill")),
                str(row["description"]),
                row["kind"],
            )
            for row in rows
        ]
        outcome = await self._fleet.ranking.aordered(
            "catalog.operation_rank", query, candidates
        )
        shown = evidence(query, ((c.id, c.name, c.description) for c in candidates))
        by_id = dict(zip((c.id for c in candidates), rows, strict=True))
        ordered: list[dict] = []
        for candidate_id in outcome.order:
            relevance = shown[candidate_id][0]
            if outcome.source == "floor" and relevance <= 0:
                continue
            ordered.append({**by_id[candidate_id], "score": round(relevance, 4)})
        return ordered[:top_k]

    async def discover_tools(
        self, query: str, top_k: int | None = None, loaded: set[str] | None = None
    ) -> dict:
        """Rank candidate tools across the whole fleet for an NL ``query``
        (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog), without exposing or holding any child.

        Backbone is the self-catalog (:meth:`probe_catalog` — each server's real
        tools, learned by a cached connect→list→release probe), ranked by token
        overlap; KG semantic-search scores are blended in when the KG is warm.
        The local-skill pseudo-server (:meth:`_local_skill_probe_info`) is
        folded in the SAME way, so a skill served from a package installed in
        this process ranks alongside fleet tools and fleet-harvested skills.
        Returns ``{"results": [...], "unavailable": {server: error}}`` so the
        caller can both pick tools and see which servers couldn't be reached.
        """
        if not top_k or top_k <= 0:
            top_k = self._fleet.config.mcp_dynamic_top_k
        catalog = self._load_catalog()
        discovery_timeout = self._fleet.config.mcp_dynamic_discovery_timeout
        deadline = asyncio.get_running_loop().time() + discovery_timeout
        fleet_probe = await self._probe._discovery_probe(
            query, catalog, discovery_timeout, deadline
        )
        local_info = self._probe._local_skill_probe_info()
        local_names = {s["name"] for s in local_info.get("skills", [])}
        probe = self._dedup_fleet_skills_against_local(fleet_probe, local_names)
        probe = {**probe, LOCAL_SKILLS_SERVER: local_info}
        rows, unavailable = await self._probe_rows(
            probe, catalog, loaded if loaded is not None else self._exposed
        )
        ranked = await self._ordered_discovery_rows(query, rows, top_k)
        # The server-level fallback (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog)
        # answers "is any FLEET server mountable", so its trigger check uses
        # ``fleet_probe`` — never the merged ``probe`` — or the local-skill
        # pseudo-server's own always-``error: None`` entry would make every
        # fleet server look reachable even when none of them actually are.
        return {
            "results": self._results_or_fallback(ranked, fleet_probe),
            "unavailable": unavailable,
        }

    async def _catalog_detail_tools(
        self, server: str, prefix: str, info: dict
    ) -> list[dict]:
        """One drilled-down server's client-visible tool rows."""
        tools = await self._tool_rows().rows(server, info, schemas=True)
        return [
            {
                "prefixed_name": prefixed_name,
                "tool": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema") or t.get("input_schema") or {},
                "enabled": self._mounts()._tool_enabled(server, t["name"]),
                # Session-scoped dispatch truth (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog):
                # derived from the SAME predicate the dispatch gate
                # (SessionVisibilityMiddleware) enforces, so this can
                # never claim a tool is usable when a call would
                # actually be rejected.
                "mounted": self._tool_dispatchable(prefixed_name),
            }
            for t in tools
            for prefixed_name in (clean_tool_name(prefix, server, t["name"]),)
        ]

    async def _catalog_server_detail(self, server: str, include_tools: bool) -> dict:
        """Drill into ONE catalog server, probing only that server."""
        info = await self._probe.probe_server(server)
        prefix = self._mounts().server_prefix(server)
        age_s, is_stale = self._probe._probe_age_and_staleness(
            info, time.time(), self._probe._probe_ttl()
        )
        result = {
            "server": server,
            "prefix": prefix,
            # Process-level fact only: the child is spawned. It does NOT
            # mean any of its tools are callable by the CALLING session —
            # that per-tool truth is the "mounted" field inside "tools"
            # below, and it is the ONLY field a caller should read to
            # decide whether it can dispatch a specific tool right now.
            "process_running": server in self.children,
            "probed": True,
            "available": info.get("error") is None,
            "error": info.get("error"),
            "age_s": age_s,
            # CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness — a
            # drill-down calls probe_server directly, whose own cache hit
            # is already TTL-gated, so this is normally fresh; still
            # reported honestly rather than assumed.
            "stale": is_stale,
        }
        if include_tools:
            result["tools"] = await self._catalog_detail_tools(server, prefix, info)
        return result

    async def _catalog_fleet_probe(self, include_tools: bool) -> dict:
        """The probe view backing a whole-fleet listing.

        A whole-fleet browse is a background sweep, not a targeted interactive
        lookup: bound it by the same interactive discovery budget as find_tools
        (a server that never answers must not hang this call indefinitely) and
        tag it BACKGROUND_INGESTION so it yields to interactive/orchestration
        work (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). A metadata-only
        listing never probes a child at all. The local-skill pseudo-server
        (:meth:`_local_skill_probe_info`) is always folded in, so a plain
        listing shows it without needing a ``find_tools`` query — the
        always-offered core pack is a subset of exactly this set.
        """
        local_info = self._probe._local_skill_probe_info()
        if not include_tools:
            return {**dict(self._probe_cache), LOCAL_SKILLS_SERVER: local_info}
        probe = await self._probe.probe_catalog(
            budget=self._fleet.config.mcp_dynamic_discovery_timeout,
            priority=_resource_priority.PriorityClass.BACKGROUND_INGESTION,
        )
        return {**probe, LOCAL_SKILLS_SERVER: local_info}

    def _catalog_tool_partition(
        self, name: str, prefix: str, tool_entries: list[dict]
    ) -> tuple[list[str], list[str]]:
        """Split one server's probed tools into (enabled, disabled) prefixed names."""
        enabled_names: list[str] = []
        disabled_names: list[str] = []
        for t in tool_entries:
            pn = clean_tool_name(prefix, name, t["name"])
            target = (
                enabled_names
                if self._mounts()._tool_enabled(name, t["name"])
                else disabled_names
            )
            target.append(pn)
        return enabled_names, disabled_names

    def _stamp_catalog_freshness(
        self, entry: dict, info: dict, now: float, ttl: float
    ) -> None:
        """Stamp honest probe freshness onto one fleet-listing entry.

        CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness — staleness is
        computed from age vs TTL, never a bare echo of the narrow in-flight
        ``stale`` flag. This is also the ``include_tools=False`` metadata-only
        path, which reads ``self._probe_cache`` directly and never goes through
        ``probe_catalog``'s re-probe targeting at all — the exact path where an
        entry could otherwise sit at ``stale: false`` no matter how old.
        """
        entry["pending"] = bool(info.get("pending"))
        if "probed_at" not in info:
            entry["stale"] = bool(info.get("stale"))
            return
        age_s, is_stale = self._probe._probe_age_and_staleness(info, now, ttl)
        entry["age_s"] = age_s
        entry["stale"] = is_stale

    async def _catalog_fleet_entry(
        self,
        name: str,
        info: dict,
        probed: bool,
        now: float,
        ttl: float,
        include_tools: bool,
    ) -> dict:
        """One server's row in a whole-fleet listing."""
        prefix = self._mounts().server_prefix(name)
        tool_entries = await self._tool_rows().rows(name, info)
        enabled_names, disabled_names = self._catalog_tool_partition(
            name, prefix, tool_entries
        )
        entry = {
            "server": name,
            "prefix": prefix,
            "tool_count": len(tool_entries),
            "enabled_count": len(enabled_names),
            # Process-level fact only (the child is spawned) — NOT a claim
            # that any tool is callable by the caller's own session. See
            # "dispatchable_tools" for the truthful, session-scoped answer.
            "process_running": name in self.children,
            "probed": probed,
            "available": info.get("error") is None if probed else None,
        }
        if probed:
            self._stamp_catalog_freshness(entry, info, now, ttl)
        if info.get("error"):
            entry["error"] = info["error"]
        if include_tools:
            entry["tools"] = enabled_names
            if disabled_names:
                entry["disabled_tools"] = disabled_names
            # Session-scoped dispatch truth (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog):
            # the subset of `tools` the CALLING session could actually
            # invoke right now, derived from the same predicate the
            # dispatch gate enforces (MCPMultiplexer.tool_dispatchable).
            entry["dispatchable_tools"] = [
                pn for pn in enabled_names if self._tool_dispatchable(pn)
            ]
        return entry

    async def list_catalog(self, server: str = "", include_tools: bool = True) -> dict:
        """Browse configured fleet metadata without unnecessary child starts.

        A server drill-down probes only that server.  A metadata-only fleet
        listing (``include_tools=False``) never probes a child and reports any
        already-cached reachability data.  Only an explicit whole-fleet tool
        listing probes the whole fleet.

        With ``server`` set, drills into one server and returns its full tool
        list with descriptions.
        """
        catalog = self._load_catalog()

        if server:
            if server not in catalog:
                return {"error": f"'{server}' is not in the catalog"}
            return await self._catalog_server_detail(server, include_tools)

        probe = await self._catalog_fleet_probe(include_tools)

        now = time.time()
        ttl = self._probe._probe_ttl()
        servers: list[dict] = [
            await self._catalog_fleet_entry(
                name, probe.get(name) or {}, name in probe, now, ttl, include_tools
            )
            for name in catalog
        ]
        return {
            "catalog_authority": "medulla",
            "total_servers": len(servers),
            "total_tools": sum(s["tool_count"] for s in servers),
            "servers_running": sorted(self.children.keys()),
            "unavailable": [s["server"] for s in servers if s["available"] is False],
            "servers": servers,
        }

    def _split_requested_tool_owners(
        self, requested_tools: list[str], servers: list[str] | None
    ) -> tuple[set[str], list[str]]:
        """(servers that must be mounted, requested names with no owning server)."""
        target_servers: set[str] = set(servers or [])
        unresolved_tools: list[str] = []
        for prefixed in requested_tools:
            owner = self._mounts().server_for_prefixed(prefixed)
            if owner:
                target_servers.add(owner)
            else:
                unresolved_tools.append(prefixed)
        return target_servers, unresolved_tools

    async def _mount_target_servers(
        self, target_servers: set[str], failed: dict[str, str]
    ) -> list[str]:
        """Mount every target; record a human-readable reason for each that didn't."""
        mounted: list[str] = []
        for server in sorted(target_servers):
            await self._mounts().mount_child(server)
            if server in self.children:
                mounted.append(server)
                continue
            # Mount failed — surface *why* via a targeted probe (cached).
            info = await self._probe.probe_server(server)
            failed[server] = info.get("error") or "could not mount (unreachable?)"
        return mounted

    def _condensed_server_surface(self, mounted: list[str]) -> set[str]:
        """The prefixed names a SERVER-level load exposes.

        CONCEPT:MERISTEM-ECO.multiplexer.condensed-server-load — only the condensed
        action surface; verbose 1:1 tools stay loadable by EXPLICIT name so
        ``load_tools(servers=[X])`` never floods a session's context with X's
        whole granular surface. Mirrors the always-on mount's verbose-hold.
        """
        wanted: set[str] = set()
        for server in mounted:
            for t in self._mounts().prefixed_tools_for_server(server):
                if _tool_is_verbose(t):
                    continue
                wanted.add(t.name)
        return wanted

    def _explain_unregistered_tools(
        self, requested_tools: list[str], failed: dict[str, str]
    ) -> None:
        """Record every requested tool whose owning server mounted but that never
        actually registered (disabled by config, or dropped by the child's
        runtime admission policy). It must not vanish silently — it belongs in
        ``failed``, not in a phantom ``newly_exposed``/``mounted`` claim."""
        for prefixed in requested_tools:
            if prefixed in self._tool_to_server() or prefixed in failed:
                continue
            owner = self._mounts().server_for_prefixed(prefixed)
            if owner is not None and owner in failed:
                continue  # already explained by the server-level failure
            failed[prefixed] = (
                "tool is not registered by its owning server "
                "(disabled by config or rejected by its runtime policy)"
            )

    async def resolve_and_mount(
        self,
        tools: list[str] | None = None,
        servers: list[str] | None = None,
    ) -> tuple[list[str], list[str], dict[str, str]]:
        """Mount whatever children are needed to satisfy a ``load_tools``
        request and compute the set of prefixed names to expose.

        Returns ``(mounted_servers, prefixed_names_to_expose, failed)`` where
        ``failed`` maps each server OR each explicitly requested tool name that
        could not be resolved to a human-readable reason (e.g. an unreachable
        remote server, or a tool the owning server never actually registered —
        disabled by config, renamed, or dropped by its own admission policy).
        A requested tool that does not end up resolvable is NEVER silently
        left out of both ``to_expose`` and ``failed`` — the caller must be
        told exactly which of its requested names didn't make it, so the
        reported state can never claim a tool is loadable when the dispatcher
        cannot actually reach it. Does NOT touch FastMCP — registration of
        the live tools (and the list_changed notification) is the caller's
        job so this stays unit-testable.
        """
        requested_tools = list(tools or [])
        target_servers, unresolved_tools = self._split_requested_tool_owners(
            requested_tools, servers
        )
        failed: dict[str, str] = {
            name: "tool is not present in the fleet catalog"
            for name in unresolved_tools
        }
        mounted = await self._mount_target_servers(target_servers, failed)
        wanted = (
            set(requested_tools)
            if requested_tools
            else self._condensed_server_surface(mounted)
        )
        to_expose = [
            name
            for name in sorted(wanted)
            if name in self._tool_to_server() and name not in self._exposed
        ]
        self._explain_unregistered_tools(requested_tools, failed)
        return mounted, to_expose, failed
