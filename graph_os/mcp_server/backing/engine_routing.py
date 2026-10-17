"""Engine routing support for graph-os's one MCP server.

The served tools, their registry (``REGISTERED_TOOLS``/``ACTION_TOOL_ROUTES``),
the dispatch core (``_execute_tool``), the process session and the engine
binding all live in :mod:`graph_os.mcp_server.runtime` and
:mod:`graph_os.mcp_server.bootstrap`. This module only holds what those tools
need on top: the multi-graph connection registry, explicit graph selection and
read fan-out, the tabular/extraction services, the tool-toggle catalog payload
the REST registry API serves, and the fleet-catalog writer bridge.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path
from typing import Any, TypedDict

from agent_utilities.core.config import setting

from graph_os.mcp_server import runtime

logger = logging.getLogger(__name__)


def _parse_skill_md_frontmatter(content: str) -> dict[str, Any]:
    """Extract the YAML frontmatter block from a SKILL.md's raw content.

    Falls back to a line-by-line ``key: value`` scan when the block is not
    valid YAML. Returns ``{}`` when there is no frontmatter block at all.
    """
    import re

    import yaml

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception:
        metadata: dict[str, Any] = {}
        for line in match.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                metadata[k.strip()] = v.strip()
        return metadata


def _skill_record_from_metadata(
    metadata: dict[str, Any], path_obj: Any
) -> dict[str, Any]:
    """Build the skill-registration record from parsed frontmatter metadata."""
    name = metadata.get("name") or path_obj.parent.name
    description = metadata.get("description") or ""
    domain = metadata.get("domain") or (
        path_obj.parent.parent.name if len(path_obj.parts) > 2 else ""
    )
    tags = metadata.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return {
        "id": name,
        "name": name,
        "description": description,
        "domain": domain,
        "tags": tags,
        "enabled": True,
        "file_path": f"skill://{name}",
    }


def _parse_skill_md(path: Any) -> dict[str, Any]:
    """Parse YAML frontmatter from a SKILL.md file."""
    from pathlib import Path

    path_obj = Path(path)
    try:
        content = path_obj.read_text(encoding="utf-8", errors="ignore")
        metadata = _parse_skill_md_frontmatter(content)
        return _skill_record_from_metadata(metadata, path_obj)
    except Exception as e:
        logger.error("Failed to parse SKILL.md: %s", e)
        name = path_obj.parent.name
        return {
            "id": name,
            "name": name,
            "description": "",
            "domain": "",
            "tags": [],
            "enabled": True,
            "file_path": f"skill://{name}",
        }


def get_toggle_states_batch(
    engine: Any, items: list[tuple[str, str]]
) -> dict[tuple[str, str], bool]:
    """Resolve many ``(item_type, item_id)`` toggle states in ONE round trip.

    DEFECT B fix: ``get_tools_endpoint`` used to call a single-item toggle read
    once per rendered item — one synchronous Cypher round trip each. Measured
    inventory on the production pod: 254 skill files + 68 skill-graph files +
    31 builtin tools + 66 MCP servers = 350+ sequential engine round trips in
    a single request (it did not return within 90s, nor within 180s). This
    batches every id the caller is about to render into ONE query.

    Engine facts this function must respect (both confirmed live against the
    deployed engine — getting either wrong makes the batch silently match
    nothing):

    1. ``STARTS WITH`` with a ``$param`` operand does not parse on the
       deployed engine. This uses ``IN`` with an explicit id list instead —
       index-servable via the engine's node-id fast path, O(items rendered)
       rather than O(all preferences), and already the pattern used by the
       sibling batching helper :func:`get_existing_disabled_batch`.
    2. The row-governance layer (``secured_reads.row_node_ids``) requires
       every returned row to carry an identity under ``id``/``node_id``/
       ``n.id``/``_id`` — this projects ``p.id AS id`` so a real match is not
       rejected by governance and silently reported as "enabled" (see
       the data-loss note above for what this caused).

    Fail-open on a query error (an id with no resolvable state defaults to
    enabled=True), matching the previous per-item
    default — this function only changes the ROUND-TRIP COUNT and the
    governance projection, not the toggle default semantics.
    """
    seen = list(dict.fromkeys(items))  # de-dupe, preserve order
    if not engine or not seen:
        return dict.fromkeys(seen, True)

    pref_id_by_key = {key: f"preference:toggle:{key[0]}:{key[1]}" for key in seen}
    pref_ids = list(pref_id_by_key.values())
    res = _toggle_batch_query(engine, pref_ids, seen)
    if res is None:
        return dict.fromkeys(seen, True)

    value_by_pref_id: dict[str, Any] = {}
    for row in res:
        if isinstance(row, dict) and row.get("id"):
            value_by_pref_id[str(row["id"])] = row.get("value")

    result: dict[tuple[str, str], bool] = {}
    for key in seen:
        value = value_by_pref_id.get(pref_id_by_key[key])
        result[key] = True if value is None else value == "enabled"
    return result


def _toggle_batch_query(
    engine: Any, pref_ids: list[str], seen: list[tuple[str, str]]
) -> list[Any] | None:
    """Run the batched ``Preference`` lookup for :func:`get_toggle_states_batch`.

    Fail-open: returns ``None`` on any query error, so the caller defaults
    every requested item to ``enabled=True``.
    """
    try:
        res = engine.query_cypher(
            "MATCH (p:Preference) WHERE p.id IN $pref_ids "
            "RETURN p.id AS id, p.value AS value",
            {"pref_ids": pref_ids},
        )
        if not isinstance(res, list):
            raise TypeError(f"expected a list of rows, got {type(res).__name__}")
    except Exception as exc:
        logger.error(
            "get_toggle_states_batch(%d items) failed — defaulting every "
            "item to enabled=True: %s",
            len(seen),
            exc,
        )
        return None
    return res


_TOGGLE_NODE_ID_PREFIX: dict[str, str] = {
    "mcp_server": "mcp_server_",
    "builtin_tool": "native_tool_",
    "skill": "skill_",
    "skill_workflow": "skill_workflow_",
    "skill_graph": "skill_graph_",
}


class _ToolsPayload(TypedDict):
    """The catalog body :func:`get_tools_endpoint` serialises.

    Named rather than ``dict[str, Any]`` so the producer/consumer seam is
    typed: the handler, its tests, and the webui contract all agree on this
    key set instead of rediscovering it from the return statement.

    FIX LANE (collapse-tool-endpoints): the original five list keys are
    UNCHANGED (same names, same per-item field names) — nothing that reads
    this route's JSON body needs to change. ``section_status`` is new and
    purely additive (CONCEPT:AU-KG.ingest.fleet-catalog-relational-tables):
    each of the five sections below now degrades independently on its own
    read failure (``"unavailable"``) instead of the whole request failing
    closed, and this map is how a caller tells "genuinely zero items" apart
    from "this section's source could not be read this time".
    """

    mcp_tools: list[dict[str, Any]]
    builtin_tools: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    skill_graphs: list[dict[str, Any]]
    skill_workflows: list[dict[str, Any]]
    section_status: dict[str, str]


# Bound on how many pages of one fleet-catalog ``kind`` this route will drain
# via registry_api's own keyset-paginated ``_authorized_page`` (100 rows per
# page, see ``registry_api._MAX_LIMIT``) before giving up on that section for
# this request. Mirrors the same defensive drain-cap idea
# ``agent_webui.api_extensions._read_fleet_catalog`` already applies to the
# identical read path (its own comment there measured ~9 pages to drain 841
# ``skills`` rows) — 25 pages is headroom above that observed size without
# letting one pathological catalog hang this request forever.
_TOOLS_CATALOG_DRAIN_MAX_PAGES = 25


def _read_catalog_kind_sync(
    kind: str, *, require_discovery_binding: bool
) -> list[dict[str, Any]]:
    """Drain one fleet-catalog ``kind`` through registry_api's OWN
    tenant/principal-scoped, fail-closed authorized-read path — the exact
    same private functions ``agent_webui.api_extensions._read_fleet_catalog``
    already reuses in-process for ``/api/enhanced/tools`` (see that
    function's docstring). This never re-derives tenant scoping, redaction,
    or SQL construction; it is a thin synchronous drain loop on top of
    ``_authorized_page``.

    Synchronous and blocking (a unix-socket engine RPC per page) by design:
    the caller, :func:`_build_tools_payload_sync`, already runs entirely
    inside a worker thread via ``asyncio.to_thread`` from
    :func:`get_tools_endpoint` — calling registry_api's own ASYNC wrapper
    (``_offload_catalog_call``, which itself does ``asyncio.to_thread``)
    from here would require a running event loop that this thread does not
    have. Calling the sync ``_authorized_page``/``_authorized_count``
    directly is therefore both correct and simpler here.

    Raises whatever ``_require_catalog_authority``/``_authorized_page``
    raise (``PermissionError``, ``registry_api.CatalogUnavailable``, or any
    other exception the engine surfaces) — the caller is responsible for
    catching this per-section and recording ``section_status``, matching
    every other section's independent-degrade contract in this function.
    """
    from graph_os.gateway.registry_api import (
        _KIND_SPECS,
        _MAX_LIMIT,
        _authorized_page,
        _get_catalog_engine,
        _require_catalog_authority,
        _row_key,
    )

    tenant, principal, grant_digests = _require_catalog_authority(
        require_discovery_binding=require_discovery_binding
    )
    engine = _get_catalog_engine()
    spec = _KIND_SPECS[kind]
    rows: list[dict[str, Any]] = []
    after: tuple[str, str] | None = None
    for _page_num in range(_TOOLS_CATALOG_DRAIN_MAX_PAGES):
        page = _authorized_page(
            kind,
            tenant=tenant,
            principal=principal,
            grant_digests=grant_digests,
            query="",
            after=after,
            limit=_MAX_LIMIT,
            engine=engine,
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < _MAX_LIMIT:
            break
        after = _row_key(spec, page[-1])
    return rows


def _gather_mcp_catalog_entries() -> tuple[list[tuple[str, dict[str, Any]]], str]:
    """Gather (name, catalog_row) pairs for the fleet catalog's ``servers`` kind.

    Section 1 of :func:`_build_tools_payload_sync` — see that function's
    docstring for why ``mcp_tools`` reads the SQL fleet catalog now.
    """
    try:
        server_rows = _read_catalog_kind_sync(
            "servers", require_discovery_binding=False
        )
        mcp_entries = [
            (str(row.get("name") or ""), row) for row in server_rows if row.get("name")
        ]
        return mcp_entries, "ok"
    except Exception as e:
        logger.error("Failed to read the fleet-catalog 'servers' kind: %s", e)
        return [], "unavailable"


def _gather_builtin_tool_stems() -> tuple[list[str], str]:
    """Gather built-in agent tool file stems. No catalog equivalent exists."""
    try:
        tools_dir = Path(__file__).resolve().parents[1] / "tools"
        builtin_stems: list[str] = []
        if tools_dir.exists() and tools_dir.is_dir():
            for f in tools_dir.glob("*.py"):
                if f.name.startswith("_"):
                    continue
                builtin_stems.append(f.stem)
        return builtin_stems, "ok"
    except Exception as e:
        logger.error("Failed to scan built-in tools directory: %s", e)
        return [], "unavailable"


def _gather_skill_and_workflow_entries(
    workspace_root: Path | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Gather Skill / Skill-Workflow entries by parsing SKILL.md files.

    Stays filesystem-sourced (not the fleet catalog) — see
    :func:`_build_tools_payload_sync`'s docstring for the domain/tags and
    freshness gap that rules the catalog out for this section.
    """
    skill_entries: list[dict[str, Any]] = []
    workflow_entries: list[dict[str, Any]] = []
    try:
        univ_skills_dir = (
            workspace_root
            / "agent-packages"
            / "skills"
            / "universal-skills"
            / "universal_skills"
            if workspace_root is not None
            else None
        )
        if univ_skills_dir is not None and univ_skills_dir.exists():
            for p in univ_skills_dir.glob("**/SKILL.md"):
                skill_info = _parse_skill_md(p)
                if "workflows" in p.parts:
                    skill_info["type"] = "Skill Workflow"
                    workflow_entries.append(skill_info)
                else:
                    skill_info["type"] = "Agent Skill"
                    skill_entries.append(skill_info)
        return skill_entries, workflow_entries, "ok"
    except Exception as e:
        logger.error("Failed to scan the universal-skills corpus: %s", e)
        return [], [], "unavailable"


def _gather_skill_graph_entries(
    workspace_root: Path | None,
) -> tuple[list[dict[str, Any]], str]:
    """Gather Skill-Graph entries by parsing SKILL.md files.

    Stays filesystem-sourced — see :func:`_build_tools_payload_sync`'s
    docstring for why (no reliable ingestion sync for this package).
    """
    graph_entries: list[dict[str, Any]] = []
    try:
        graphs_dir = (
            workspace_root
            / "agent-packages"
            / "skills"
            / "skill-graphs"
            / "skill_graphs"
            if workspace_root is not None
            else None
        )
        if graphs_dir is not None and graphs_dir.exists():
            for p in graphs_dir.glob("**/SKILL.md"):
                skill_info = _parse_skill_md(p)
                skill_info["type"] = "Skill Graph"
                graph_entries.append(skill_info)
        return graph_entries, "ok"
    except Exception as e:
        logger.error("Failed to scan the skill-graphs corpus: %s", e)
        return [], "unavailable"


def _resolve_tool_payload_toggle_states(
    engine: Any,
    mcp_entries: list[tuple[str, dict[str, Any]]],
    builtin_stems: list[str],
    workflow_entries: list[dict[str, Any]],
    skill_entries: list[dict[str, Any]],
    graph_entries: list[dict[str, Any]],
) -> dict[tuple[str, str], bool]:
    """One batched toggle-state round trip for every item about to render.

    See :func:`_build_tools_payload_sync`'s docstring for why ``mcp_tools``
    reads this Preference-node store even though it is catalog-sourced now.
    """
    toggle_keys: list[tuple[str, str]] = (
        [("mcp_server", name) for name, _row in mcp_entries]
        + [("builtin_tool", stem) for stem in builtin_stems]
        + [("skill_workflow", info["id"]) for info in workflow_entries]
        + [("skill", info["id"]) for info in skill_entries]
        + [("skill_graph", info["id"]) for info in graph_entries]
    )
    return get_toggle_states_batch(engine, toggle_keys)


def _mcp_tools_section(
    mcp_entries: list[tuple[str, dict[str, Any]]],
    toggle_states: dict[tuple[str, str], bool],
) -> list[dict[str, Any]]:
    """Build the ``mcp_tools`` payload rows from catalog entries + toggle state."""
    mcp_tools: list[dict[str, Any]] = []
    for name, row in mcp_entries:
        mcp_enabled = toggle_states[("mcp_server", name)]
        if not row.get("enabled", True):
            mcp_enabled = False
        transport = str(row.get("transport") or "")
        is_stdio = transport == "stdio"
        mcp_tools.append(
            {
                "name": name,
                "type": "MCP Server",
                "launch_mode": "subprocess" if is_stdio else "remote",
                # The catalog never stores the raw command/args (privacy —
                # see fleet_catalog_tables' module docstring); these stayed
                # opaque presence markers even before this migration.
                "command": "[configured]" if is_stdio else "",
                "args": ["[configured]"] if is_stdio else [],
                "status": "active" if mcp_enabled else "disabled",
                "enabled": mcp_enabled,
            }
        )
    return mcp_tools


def _build_tools_payload_sync(
    engine: Any, workspace_root: Path | None
) -> _ToolsPayload:
    """Synchronous body of :func:`get_tools_endpoint` — file I/O + ONE batched engine round trip.

    DEFECT A/B fix: this used to be inlined directly in the ``async def``
    handler, issuing a single-item toggle read per rendered item (350+
    sequential, BLOCKING ``query_cypher`` round trips on the production pod —
    254 skill files + 68 skill-graph files + 31 builtin tools + 66 MCP
    servers — enough that the request never returned within 180s). Every one
    of those blocking calls ran directly on the single asyncio event loop,
    starving every other request on the worker (reproduced live: concurrent
    static-asset requests timed out at the 25s ceiling while this request was
    in flight; idle baseline for those same assets is 44-112ms).

    Fixed two ways:
    1. This whole function is now synchronous, blocking, file-I/O-and-engine
       heavy code, run via ``asyncio.to_thread`` from the async endpoint
       below — matching the existing ``_execute_tool``/``asyncio.to_thread``
       pattern already used elsewhere in this file — so it never blocks the
       event loop.
    2. It gathers every ``(item_type, item_id)`` pair it is about to render
       FIRST, then resolves every toggle state in ONE
       :func:`get_toggle_states_batch` call instead of N per-item calls.

    FIX LANE (collapse-tool-endpoints) — SQL fleet catalog as the single
    source of truth: this used to build every section from a fresh
    config/filesystem scan, a second inventory of the SAME MCP/skill fleet
    that ``/api/registry/*`` and the webui BFF already read from the SQL
    fleet-catalog tables (``agent_utilities.knowledge_graph.core.
    fleet_catalog_tables``). Evidence-based per section:

    - ``mcp_tools`` (despite the key name, this has always been a list of
      *servers*, one per configured ``mcpServers`` entry — never individual
      MCP tools) now reads the catalog's ``servers`` kind
      (``mcp_servers`` table). That table is written from the SAME
      multiplexer config map (``MCPMultiplexer.load_catalog()``) this used
      to re-parse from ``mcp_config.json`` directly
      (:func:`~..knowledge_graph.core.fleet_catalog_tables.
      write_fleet_catalog`), so this is a genuine single-source collapse
      with no fidelity loss: ``command``/``args`` were already opaque
      presence markers (``"[configured]"``), never real values, and the
      catalog derives the same ``launch_mode`` split from ``transport``
      that this used to derive from ``cfg.get("command")``.
    - ``skills``/``skill_workflows``/``skill_graphs``/``builtin_tools``
      stay on their existing filesystem/KG-native sources — investigated
      and deliberately NOT moved:
        * ``builtin_tools`` has no catalog table at all. The fleet catalog
          models MCP servers/tools/prompts/resources and skills-over-MCP;
          these are native, in-process Python callables under
          ``agent_utilities/tools/*.py``, never MCP-discovered and never
          written to any catalog table.
        * ``skills``/``skill_workflows`` (local ``universal-skills``
          corpus) — the catalog's ``skills`` table CAN represent an
          individual skill's id/name/description/enabled (written by
          :func:`~..knowledge_graph.ingestion.skill_workflow_ingest.
          ingest_atomic_skills`/``ingest_skill_workflows``), but it does
          NOT store ``domain`` or ``tags`` — both real fields on this
          route's existing per-item shape, sourced from each ``SKILL.md``'s
          frontmatter. There is also no live-freshness guarantee: catalog
          rows are only as current as the last ingestion pass (an
          on-demand action or the package-install-triggered watermarked
          leg), while this filesystem glob always reflects the corpus as
          it exists on disk right now. Moving these two sections would
          silently blank ``domain``/``tags`` and could show a stale/absent
          item for anything added since the last ingest — exactly the
          "fabricate or silently drop" failure mode this fix lane was
          told to avoid, so they stay filesystem-sourced.
        * ``skill_graphs`` — the catalog schema supports this
          (``skill_type="graph"``), but unlike the atomic-skill/workflow
          legs, nothing ingests the ``skill-graphs`` package on any
          automatic/scheduled trigger (only a manual, explicit-``root``
          on-demand action reaches it) — in a typical deployment those
          catalog rows are simply absent. Serving this section from the
          catalog today would silently show an empty list where the
          on-disk corpus is real and current, so it also stays
          filesystem-sourced.
    """

    mcp_entries, mcp_status = _gather_mcp_catalog_entries()
    builtin_stems, builtin_status = _gather_builtin_tool_stems()
    skill_entries, workflow_entries, skills_status = _gather_skill_and_workflow_entries(
        workspace_root
    )
    graph_entries, graphs_status = _gather_skill_graph_entries(workspace_root)
    section_status: dict[str, str] = {
        "mcp_tools": mcp_status,
        "builtin_tools": builtin_status,
        "skills": skills_status,
        "skill_workflows": skills_status,
        "skill_graphs": graphs_status,
    }

    # ── ONE batched engine round trip for every toggle state ───────────────
    # Still the Preference-node toggle store, for EVERY section including the
    # now-catalog-sourced ``mcp_tools`` — this is deliberate, not an
    # oversight: ``POST /api/tools/toggle`` (``toggle_tool_endpoint`` below)
    # writes user enable/disable preference to this SAME store, keyed by
    # ``(item_type, item_id)``. The fleet-catalog row's own ``enabled``
    # column reflects the SERVER's configured ``disabled`` flag, not this
    # per-user toggle preference — reading catalog ``enabled`` here instead
    # would make toggling a server in the UI silently stop being reflected
    # on the next GET. The catalog row's own ``enabled`` is still honored as
    # an additional AND term below (a server force-disabled in config stays
    # disabled even if the toggle preference says otherwise), preserving the
    # original ``cfg.get("disabled")`` override semantics.
    toggle_states = _resolve_tool_payload_toggle_states(
        engine,
        mcp_entries,
        builtin_stems,
        workflow_entries,
        skill_entries,
        graph_entries,
    )

    mcp_tools = _mcp_tools_section(mcp_entries, toggle_states)

    builtin_tools = [
        {
            "name": stem,
            "type": "Built-in Tool",
            "file_path": f"tool://{stem}",
            "status": "enabled"
            if toggle_states[("builtin_tool", stem)]
            else "disabled",
            "enabled": toggle_states[("builtin_tool", stem)],
        }
        for stem in builtin_stems
    ]

    workflows = []
    for skill_info in workflow_entries:
        skill_info["enabled"] = toggle_states[("skill_workflow", skill_info["id"])]
        workflows.append(skill_info)

    skills = []
    for skill_info in skill_entries:
        skill_info["enabled"] = toggle_states[("skill", skill_info["id"])]
        skills.append(skill_info)

    graphs = []
    for skill_info in graph_entries:
        skill_info["enabled"] = toggle_states[("skill_graph", skill_info["id"])]
        graphs.append(skill_info)

    return {
        "mcp_tools": mcp_tools,
        "builtin_tools": builtin_tools,
        "skills": sorted(skills, key=lambda x: x.get("name", "").lower()),
        "skill_graphs": sorted(graphs, key=lambda x: x.get("name", "").lower()),
        "skill_workflows": sorted(workflows, key=lambda x: x.get("name", "").lower()),
        "section_status": section_status,
    }


_TABULAR_QUERY_SERVICE: Any = None
_TABULAR_QUERY_SERVICE_KEY: tuple[str, str, str] | None = None
_TABULAR_QUERY_SERVICE_LOCK = threading.Lock()


def _trino_principal_ref() -> str:
    """Resolve the current verified caller's opaque persistence reference."""

    from agent_utilities.security.brain_context import current_actor
    from agent_utilities.security.persistence_privacy import persistence_reference

    actor = current_actor()
    actor.ensure_credential_current()
    if not actor.authenticated or not str(actor.actor_id or "").strip():
        raise PermissionError("verified Trino principal identity is unavailable")
    return persistence_reference("delegated_actor", actor.actor_id)


def _trino_composition_settings() -> tuple[str, str, str]:
    """Read and validate the process-owned Trino composition settings."""

    from agent_utilities.core.config import config as runtime_config

    if not runtime_config.enable_delegation:
        raise RuntimeError("Trino queries require delegated authentication")
    endpoint = str(runtime_config.trino_endpoint or "").strip()
    catalog = str(runtime_config.lakekeeper_warehouse or "").strip()
    audience = str(runtime_config.delegation_audience or "").strip()
    if not endpoint:
        raise RuntimeError("Trino endpoint is not configured (TRINO_ENDPOINT)")
    if not catalog:
        raise RuntimeError("Trino catalog is not configured (LAKEKEEPER_WAREHOUSE)")
    if not audience:
        raise RuntimeError("Trino delegation audience is not configured (AUDIENCE)")
    return endpoint, catalog, audience


def get_tabular_query_service() -> Any:
    """Compose the one tabular query application service for MCP/REST callers.

    Process configuration is bound here, above the backend and application
    layers. Credential providers remain request-scoped callables and are
    resolved by the backend on every dispatch.
    """

    from agent_utilities.knowledge_graph.backends.trino_backend import (
        TrinoQueryBackend,
    )
    from agent_utilities.knowledge_graph.core.tabular_query_service import (
        TabularQueryService,
    )

    from graph_os.mcp_server.backing.delegated_auth import get_delegated_token

    key = _trino_composition_settings()
    global _TABULAR_QUERY_SERVICE, _TABULAR_QUERY_SERVICE_KEY
    previous: Any = None
    with _TABULAR_QUERY_SERVICE_LOCK:
        if _TABULAR_QUERY_SERVICE is not None and _TABULAR_QUERY_SERVICE_KEY == key:
            return _TABULAR_QUERY_SERVICE
        endpoint, catalog, audience = key
        backend = TrinoQueryBackend(
            endpoint,
            catalog=catalog,
            token_provider=lambda: get_delegated_token(audience=audience),
            principal_ref_provider=_trino_principal_ref,
        )
        service = TabularQueryService(backend)
        previous = _TABULAR_QUERY_SERVICE
        _TABULAR_QUERY_SERVICE = service
        _TABULAR_QUERY_SERVICE_KEY = key
    if previous is not None:
        try:
            previous.close()
        except Exception:  # noqa: BLE001 - replacement is already fail-closed
            logger.warning("previous tabular query service close failed", exc_info=True)
    return service


_EXTRACTION_MANAGER: Any = None


def _get_extraction_manager(engine: Any) -> Any:
    """Lazily build the single GPU-slot extraction job manager (KG-2.65)."""
    global _EXTRACTION_MANAGER
    if _EXTRACTION_MANAGER is None:
        from agent_utilities.knowledge_graph.extraction.job_manager import (
            ExtractionJobManager,
        )

        _EXTRACTION_MANAGER = ExtractionJobManager(engine)
    return _EXTRACTION_MANAGER


# ── CONCEPT:AU-KG.backend.multi-connection-registry — Named multi-connection graph registry ────────────────
_CONNECTION_REGISTRY = None
_REGISTRY_LOCK = threading.Lock()


def get_connection_registry():
    """Process-wide :class:`ConnectionRegistry` singleton.

    The reserved ``"default"`` target resolves only the process-active authority;
    registry construction never creates or seeds a second engine. Reference-only
    ``config.external_graph_connectors`` and ``config.kg_connections`` are
    registered on first build.
    """
    global _CONNECTION_REGISTRY
    if _CONNECTION_REGISTRY is not None:
        return _CONNECTION_REGISTRY
    with _REGISTRY_LOCK:
        if _CONNECTION_REGISTRY is not None:
            return _CONNECTION_REGISTRY
        from agent_utilities.knowledge_graph.core.connection_registry import (
            ConnectionRegistry,
        )

        registry = ConnectionRegistry()
        # Seed reference-only external sources first, then let an explicit
        # KG_CONNECTIONS declaration with the same alias take precedence.
        _seed_connection_registry(registry)
        _CONNECTION_REGISTRY = registry
        return _CONNECTION_REGISTRY


def _seed_external_graph_connectors(registry: Any) -> None:
    """Register reference-only external sources from config, best-effort per item."""
    from agent_utilities.core.config import config as _cfg

    for declared in _cfg.external_graph_connectors or []:
        value = (
            declared.model_dump() if hasattr(declared, "model_dump") else dict(declared)
        )
        name = str(value.pop("name", "") or "")
        if not name:
            continue
        value["role"] = "read"
        try:
            registry.register(name, value)
        except Exception as exc:  # noqa: BLE001 — one bad declaration never blocks the rest
            logger.warning(
                "Skipping invalid external source declaration: %s",
                type(exc).__name__,
            )


def _seed_kg_connections(registry: Any) -> None:
    """Register explicit KG_CONNECTIONS declarations from config, best-effort per item."""
    from agent_utilities.core.config import config as _cfg

    for spec in _cfg.kg_connections or []:
        spec = dict(spec)
        name = spec.pop("name", "")
        if name:
            try:
                registry.register(name, spec)
            except Exception as e:  # noqa: BLE001 — one bad declaration never blocks the rest
                logger.warning(
                    "Skipping invalid graph connection declaration: %s",
                    type(e).__name__,
                )


def _seed_connection_registry(registry: Any) -> None:
    """Seed a fresh :class:`ConnectionRegistry` from config, best-effort.

    An explicit ``KG_CONNECTIONS`` declaration takes precedence over a
    reference-only external source registered under the same alias, since
    external sources are seeded first. Config-less environments (the whole
    seeding step raises) leave the registry with nothing seeded.
    """
    try:
        _seed_external_graph_connectors(registry)
        _seed_kg_connections(registry)
    except Exception as exc:  # noqa: BLE001 — config-less environments
        logger.debug(
            "Graph connection declarations were not seeded (%s)",
            type(exc).__name__,
        )


def _resolve_target_engines(
    target: Any,
) -> tuple[list[tuple[str, Any]], dict[str, str], bool]:
    """Resolve a tool ``target`` into live engines for execution.

    Returns ``(entries, errors, fanout)`` where ``entries`` is a list of
    ``(name, engine)`` to run against and ``errors`` maps any name that could not
    be resolved to its error string. For a non-fan-out target, resolution errors
    propagate (fail-loud); for fan-out they are captured into ``errors`` so one
    bad connection never aborts the others (partial-success contract).
    """
    registry = get_connection_registry()
    names, fanout = registry.resolve_names(target)
    entries: list[tuple[str, Any]] = []
    errors: dict[str, str] = {}
    for name in names:
        if fanout:
            engine, err = registry.safe_get_engine(name)
            if err is not None:
                errors[name] = err
            else:
                entries.append((name, engine))
        else:
            entries.append((name, registry.get_engine(name)))
    return entries, errors, fanout


def _resolve_read_engines(
    target: Any,
) -> tuple[list[tuple[str, Any]], dict[str, str], bool]:
    """Resolve a READ tool's ``target`` into engines, unioning content graphs.

    CONCEPT:AU-KG.ingest.unified-query-routing — preserve unified query under ingestion graph routing. When
    routing is on and the caller did NOT pin an explicit target, content lives
    spread across per-source graphs (``code:*`` / ``src:*`` / …) that the single
    default engine cannot see. This resolver returns one engine per active content
    graph (plus the default) with ``fanout=True``, so the existing fan-out machinery
    unions them and a node written to ``code:X`` stays findable via the normal
    ``graph_search`` / ``graph_query`` path. An explicit ``target`` (a named
    connection, ``"all"``, a list) defers to the standard connection resolver
    unchanged, and with routing off this is byte-for-byte ``_resolve_target_engines``.
    """
    from agent_utilities.knowledge_graph.core import ingest_routing

    is_implicit_default = target is None or (
        isinstance(target, str) and target.strip().lower() in ("", "default")
    )
    if not is_implicit_default:
        return _resolve_target_engines(target)

    read_graphs = ingest_routing.read_graph_targets()
    if len(read_graphs) <= 1:
        # Nothing routed yet → stay on the fast single default-graph path.
        return _resolve_target_engines(target)

    from agent_utilities.knowledge_graph.core.shard_topology import default_graph_name

    default_graph = default_graph_name()
    entries: list[tuple[str, Any]] = []
    errors: dict[str, str] = {}
    # CONCEPT:AU-KG.backend.fanout-dedup — de-duplicate fan-out targets by the engine's actual bound
    # graph so the SAME backend (e.g. ``__commons__``) is never queried more than
    # once. Without this a query for nodes that live only in the default graph is
    # answered identically by every target, and an aggregation row (no node id to
    # dedup on) is repeated once per graph. Key on the backend's ``graph_name``,
    # falling back to ``id(engine)`` so two engines over one store collapse to one.
    seen_backends: set[Any] = set()

    def _backend_key(engine: Any) -> Any:
        gname = getattr(getattr(engine, "backend", None), "graph_name", None)
        return gname if gname is not None else id(engine)

    for gname in read_graphs:
        if gname == default_graph:
            eng: Any = runtime._get_engine()
            name = "default"
        else:
            eng, err = ingest_routing.safe_engine_for_graph(gname)
            if err is not None:
                errors[gname] = err
                continue
            name = gname
        key = _backend_key(eng)
        if key in seen_backends:
            continue
        seen_backends.add(key)
        entries.append((name, eng))
    return entries, errors, True


class GraphSelectionError(ValueError):
    """Base for an explicit ``graph`` selection that cannot be honored.

    CONCEPT:AU-KG.backend.explicit-graph-selection — ``connection`` (a backend
    alias, CONCEPT:AU-KG.backend.multi-connection-registry, resolved by
    :class:`~agent_utilities.knowledge_graph.core.connection_registry.ConnectionRegistry`)
    and ``graph`` (a physical engine graph, the thing ``engine_tenants(action="list")``/
    the engine's own ``ListGraphs`` returns) are two independent axes. Every raise
    of this family is FAIL-CLOSED: the caller gets a typed error, never a silent
    fallback to a default graph and never a union across graphs.
    """


class GraphNotFoundError(GraphSelectionError, LookupError):
    """The requested ``graph`` is absent from the engine's own catalog."""


class GraphSelectionConflictError(GraphSelectionError):
    """The ``graph``/``connection`` combination is ambiguous or unsupported.

    Raised instead of silently picking one interpretation — e.g. an explicit
    ``graph`` alongside a fan-out ``connection`` (``'all'``/a list), or an
    explicit ``graph`` alongside a non-native (external/read-only) connection
    that has no physical-graph concept of its own.
    """


def resolve_explicit_graph(
    entries: list[tuple[str, Any]], graph: str, *, fanout: bool
) -> list[tuple[str, Any]]:
    """Validate an explicit ``graph`` against the already-resolved connection(s).

    Returns ``entries`` unchanged (no-op) when ``graph`` is empty — existing
    ``connection``-only behavior is untouched. When ``graph`` is set:

    * a fan-out ``connection`` (``entries`` has more than one, or ``fanout`` is
      True) is rejected — an explicit graph requires exactly one connection,
      never a union (CONCEPT:AU-KG.backend.explicit-graph-selection).
    * a non-``"default"`` connection is rejected — only the native (epistemic-
      graph) connection has a multi-graph concept; naming a graph for e.g. an
      external Neo4j connection would otherwise be silently ignored (a silent
      pick of that connection's own implicit graph), which is exactly the
      defect this function exists to prevent.
    * existence is checked, best-effort, against the SAME catalog
      ``engine_tenants(action="list")``/``ListGraphs`` already exposes
      (``engine.graph_compute.client.tenants.list()``) — never a parallel
      authorization mechanism. The actual per-call authorization remains the
      engine's own RBAC/RLS (``crates/eg-core/src/isolation.rs::check_access``),
      evaluated server-side on every request this binds into, regardless of
      whether this probe ran.
    """
    graph = graph.strip() if isinstance(graph, str) else ""
    if not graph:
        return entries
    if fanout or len(entries) != 1:
        raise GraphSelectionConflictError(
            "an explicit graph requires exactly one resolved connection, "
            "never a fan-out/union"
        )
    name, engine = entries[0]
    if name != "default":
        raise GraphSelectionConflictError(
            f"connection {name!r} has no physical-graph concept; explicit "
            "graph selection is supported only on the default connection"
        )
    _validate_graph_exists_in_catalog(engine, graph)
    return entries


def _validate_graph_exists_in_catalog(engine: Any, graph: str) -> None:
    """Best-effort existence check for ``graph`` against the engine's catalog.

    A degraded/unavailable catalog probe must never itself deny or (worse)
    silently permit — the engine's own RBAC/RLS is the real authorization
    boundary either way, so this just skips the check in that case and lets
    the actual call surface whatever the engine decides.
    """
    tenants = getattr(
        getattr(getattr(engine, "graph_compute", None), "client", None),
        "tenants",
        None,
    )
    list_graphs = getattr(tenants, "list", None)
    if not callable(list_graphs):
        return
    try:
        catalog = list_graphs() or []
    except Exception:  # noqa: BLE001 — best-effort probe; see docstring
        return
    names = {
        row.get("name") for row in catalog if isinstance(row, dict) and row.get("name")
    }
    if graph not in names:
        raise GraphNotFoundError(
            f"graph {graph!r} is not present in the engine catalog"
        )


@contextlib.contextmanager
def bound_to_graph(graph: str) -> Any:
    """Bind an explicit physical graph onto the verified session for one call.

    A no-op when ``graph`` is empty. Otherwise reuses the SAME sanctioned
    session-retargeting primitive already used by
    ``agent_utilities.knowledge_graph.pipeline`` and
    ``agent_utilities.orchestration.approval`` —
    :meth:`~graph_os.api.contracts.session.GraphSession.with_graph`
    scoped for the call's duration via
    :func:`~graph_os.api.contracts.session.use_session`. This
    introduces no new authority: the engine's own RBAC/RLS
    (``crates/eg-core/src/isolation.rs::check_access``) evaluates
    ``(agent_id, graph, action)`` on every RPC issued while bound, independent
    of what this function does — binding only says which graph the request
    NAMES, never what it is ALLOWED to touch.
    """
    graph = graph.strip() if isinstance(graph, str) else ""
    if not graph:
        yield
        return
    from graph_os.api.contracts.session import (
        current_session,
        use_session,
    )

    session = current_session()
    if session is None:
        raise GraphSelectionConflictError(
            "a verified session is required to bind an explicit graph"
        )
    with use_session(session.with_graph(graph)):
        yield


#: Per-target wall-clock budget (seconds) for a fan-out (``target='all'`` or a
#: multi-target list). One slow/unreachable backend must not stall the whole set;
#: override live via ``graph_config set GRAPH_FANOUT_TIMEOUT`` (KG-2.63).
DEFAULT_FANOUT_TIMEOUT_S = 30.0

#: Per-target wall-clock budget (seconds) for an IMPLICIT-default read fan-out —
#: a ``graph_search``/``graph_query`` call with no explicit ``target`` that
#: resolves (CONCEPT:AU-KG.ingest.unified-query-routing) to the routed
#: content-graph union: ``default`` + every active ``code:*``/``src:*`` graph,
#: which can be dozens of per-repo connections and often includes idle/
#: unreachable ones. Using the full ``DEFAULT_FANOUT_TIMEOUT_S`` there means one
#: unreachable ``code:<repo>`` backend blocks the common no-target call for up to
#: 30s each, flooding the result with "timed out" entries. A short budget keeps
#: the default call fast — an unreachable graph is skipped, not waited on — while
#: an explicit ``target='all'``/list (a deliberate cross-repo search) keeps the
#: full ``DEFAULT_FANOUT_TIMEOUT_S``.
DEFAULT_CONTENT_FANOUT_TIMEOUT_S = 3.0


def fanout_error_label(exc: BaseException) -> str:
    """Classify a per-target fan-out failure as retryable-degraded or rejected.

    BUG-048: ``fanout_execute`` (below) and the "primary target" call sites
    that deliberately bypass its concurrency machinery but must mirror its
    error handling (e.g. ``graph_query``'s ``default``-connection fast path
    in ``query_tools.py``) reduce a raised exception to a bare string outside
    the ``OperationResult``/``OperationError`` schema ``public_error_payload``
    covers. Without this, a breaker-open on one fan-out target collapsed to
    the SAME ``"target_operation_failed"`` label as a genuinely rejected
    query — indistinguishable to a caller deciding whether to retry, exactly
    the defect BUG-048 fixed at the single-target boundary. Reuses
    :func:`agent_utilities.security.error_surface.is_engine_degraded` — the
    one place this breaker-vs-rejection vocabulary is defined — rather than
    reimplementing it a third time.
    """
    from agent_utilities.security.error_surface import is_engine_degraded

    return "target_degraded" if is_engine_degraded(exc) else "target_operation_failed"


def fanout_execute(entries, fn, *, timeout=None):
    """Run ``fn(name, engine)`` for every fan-out target CONCURRENTLY under a shared
    per-target wall-clock timeout, so one slow/unreachable backend can't stall the
    others (CONCEPT:AU-KG.backend.multi-connection-registry).

    Returns ``(results, errors)`` keyed by connection name. A target that exceeds the
    budget (or raises) lands in ``errors`` while the rest still return — the
    partial-success contract the sequential loop violated by blocking on the slowest.
    A raised target is labeled via :func:`fanout_error_label` — ``"target_degraded"``
    for a breaker-open/transport-down failure (retryable), ``"target_operation_failed"``
    for everything else (BUG-048) — never the exception text itself.

    B-18: ``concurrent.futures.ThreadPoolExecutor.submit`` does NOT propagate the
    calling thread's :mod:`contextvars` context — unlike ``asyncio.to_thread``
    (which the async tool endpoints above use to reach this synchronous helper
    in the first place), a plain worker thread starts with a FRESH, empty
    context. Without this, the ambient authenticated ``GraphSession`` (and any
    narrowing a caller applies via ``bound_to_graph`` around ``fn``) is
    invisible inside every fan-out target, and each one fails closed with
    ``SessionRequiredError`` regardless of which graph it targets. Capturing
    ``contextvars.copy_context()`` once PER submission (never reused across
    concurrent ``Context.run`` calls, which is not reentrant) and running
    ``fn`` inside that copy restores the ambient session per target while
    keeping each target's own narrowing (if any) isolated from its siblings.
    """
    import concurrent.futures
    import contextvars

    if timeout is None:
        timeout = float(setting("GRAPH_FANOUT_TIMEOUT", DEFAULT_FANOUT_TIMEOUT_S))
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if not entries:
        return results, errors
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(entries)))
    futures = {
        ex.submit(contextvars.copy_context().run, fn, name, engine): name
        for name, engine in entries
    }
    done, not_done = concurrent.futures.wait(futures, timeout=timeout)
    for fut in done:
        name = futures[fut]
        try:
            results[name] = fut.result()
        except Exception as exc:  # noqa: BLE001 — partial-success contract
            logger.warning(
                "Graph fan-out target failed (exception_type=%s)",
                type(exc).__name__,
            )
            errors[name] = fanout_error_label(exc)
    for fut in not_done:
        errors[futures[fut]] = "target_timeout"
    # Never block on a hung backend's thread; let it finish in the background.
    ex.shutdown(wait=False, cancel_futures=True)
    return results, errors


def _ontology_system():
    """Return an OntologySystem bound to the live engine store (or offline).

    Module-level so the ontology/object tool group registers from
    mcp/tools/ontology_tools.py instead of a _build_server closure.
    """
    from agent_utilities.knowledge_graph.facade import KnowledgeGraph

    try:
        engine = runtime._get_engine()
    except Exception:  # pragma: no cover - defensive
        engine = None
    backend = getattr(engine, "backend", None) if engine is not None else None
    kg = KnowledgeGraph()
    if backend is not None:
        kg._store = backend
    return kg.ontology

    # Fleet tool schemas stay lazy.  Startup has already materialized each MCP
    # server declaration above; probing every child here would launch the whole
    # fleet and contend with an operator's targeted ``list_catalog`` call.
    # Explicit ``source_sync(source="fleet")`` remains the governed full-scan
    # path when an operator wants every live tool schema elevated into the KG.


# ── Boot hydration plan (ingestion-hydration-program.md §3) ─────────────────
#
# ``_ingest_capabilities`` above (mcp_config.json / native tools / skills) is
# the ORIGINAL boot hydration; the two helpers below extend it with the
# capability legs Phases C and E built but never wired to a boot call. Each is
# its own best-effort, exception-isolated step — same shape as the ontology
# federation sync already nested inside :func:`_start_engine_bootstrap`'s
# background thread — so a failure in one never skips, or blocks serving for,
# the others. Fleet tool-schema ingestion (Phase A) and the mcp_config.json
# router (Phase B) need no boot call here: A rides its own hourly
# ``deploy/schedules.yml`` cadence (``fleet-tool-schema-sync``) and B rides the
# always-on codebase sweep's ``_route_classified_artifacts`` fan-out.


# ══════════════════════════════════════════════════════════════════
