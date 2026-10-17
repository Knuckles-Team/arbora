"""graph_argument — AIF (Argument Interchange Format) MCP tool.

CONCEPT:AU-KG.epistemic.aif.

One action-routed surface over
:mod:`agent_utilities.knowledge_graph.argumentation.aif` — the typed AIF
interchange vocabulary (I-nodes/RA/CA/PA, plus AIF+ TA/YA) layered over the
engine's existing Claim/Evidence/BeliefState + Dung argumentation
(``eg-epistemic``'s ``Method::ResolveConflict``). Actions:

* ``import_aif`` — parse AIFdb-shaped JSON and write it into the KG via the
  shared ChangeEnvelope/ingest path (``aif.import_argument_map``).
* ``export_aif`` — read a previously-imported map back out by its
  ``map_id`` and render it as AIFdb-shaped JSON (``aif.export_argument_map``
  + ``aif.to_aifdb_json``).
* ``evaluate`` — Dung acceptability: project the map's CA/PA structure
  (``aif.to_dung``) and hand its I-node ids to the REAL engine argumentation
  solver via the SAME ``engine_tools._dispatch`` dispatcher
  ``graph_epistemic``'s own ``resolve_conflict`` action already uses — no
  grounded/preferred/stable solver is reimplemented here.
* ``add_scheme`` — register a new named AIF Scheme template (the Forms-
  Ontology side) so a future RA/CA/PA-node can fulfil it.

Mirrors the ``graph_ops_causal``/``graph_epistemic`` action-router shape
(single ``@mcp.tool``, an ``action`` enum, JSON payload fields, registered
into ``runtime.REGISTERED_TOOLS``) rather than inventing a new tool
convention.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from graph_os.api.errors import not_served
from graph_os.mcp_server import runtime

_ACTIONS: tuple[str, ...] = ("import_aif", "export_aif", "evaluate", "add_scheme")


def _parse_map_json(argument_map_json: str, map_id: str) -> Any:
    raise not_served("knowledge_graph.argumentation.aif", "EG-FEDERATED-QUERY-R700")


def _evaluate(
    argument_map: Any, node_ids: list[str], semantics: str, graph: str
) -> dict[str, Any]:
    """Project ``argument_map`` (if given) via ``aif.to_dung`` and delegate the
    actual acceptability computation to the engine's ``resolve_conflict``
    method through the SAME dispatcher ``graph_epistemic`` uses.
    """
    raise not_served("knowledge_graph.argumentation.aif", "EG-FEDERATED-QUERY-R700")


def register_argument_tools(mcp: Any) -> None:
    """Register the ``graph_argument`` group on the given FastMCP server."""

    @mcp.tool(
        name="graph_argument",
        description=(
            "AIF (Argument Interchange Format) argument maps: I-nodes (claims) "
            "linked through S-nodes — RA (rule-of-inference application), CA "
            "(conflict application), PA (preference application), plus the "
            "AIF+ TA (transition)/YA (illocutionary) dialogue extensions. "
            "Layers the AIF interchange vocabulary over the engine's existing "
            "Claim/Evidence/BeliefState + Dung argumentation (eg-epistemic "
            "Method::ResolveConflict) rather than a second store: importing "
            "an RA/CA-node also writes the underlying SUPPORTS/ATTACKS edge "
            "so acceptability is computed by the real engine solver. Actions: "
            "'import_aif' (argument_map_json [+ optional map_id] -> parse "
            "AIFdb-shaped JSON {nodes,edges} and write it via the shared "
            "ChangeEnvelope/ingest path), 'export_aif' (map_id -> read a "
            "previously-imported map back out as AIFdb-shaped JSON), "
            "'evaluate' (argument_map_json and/or node_ids [+ optional "
            "semantics='grounded'|'preferred'|'stable'] -> Dung acceptability "
            "via the engine's resolve_conflict, annotated with the map's own "
            "CA/PA-derived attack/preference projection), 'add_scheme' "
            "(scheme_name + scheme_kind='inference'|'conflict'|'preference' "
            "[+ optional description/scheme_id] -> register a new named AIF "
            "Scheme template)."
        ),
        tags=["graph-os", "argumentation", "aif", "epistemic", "engine"],
    )
    def graph_argument(
        action: str = Field(
            default="evaluate",
            description="import_aif | export_aif | evaluate | add_scheme",
        ),
        argument_map_json: str = Field(
            default="",
            description='Inline AIFdb-shaped JSON: {"nodes": [{"nodeID","text",'
            '"type"}, ...], "edges": [{"edgeID","fromID","toID"}, ...]} '
            "(import_aif, evaluate).",
        ),
        map_id: str = Field(
            default="",
            description="Argument-map id: required for export_aif, optional "
            "override for import_aif, optional scope for evaluate.",
        ),
        node_ids: str = Field(
            default="[]",
            description="JSON array of already-imported I-node ids to "
            "evaluate (evaluate; alternative/addition to argument_map_json).",
        ),
        semantics: str = Field(
            default="grounded",
            description="Dung semantics for evaluate: grounded | preferred | stable.",
        ),
        scheme_name: str = Field(
            default="", description="Scheme display name (add_scheme)."
        ),
        scheme_kind: str = Field(
            default="", description="inference | conflict | preference (add_scheme)."
        ),
        description: str = Field(
            default="", description="Optional scheme description (add_scheme)."
        ),
        scheme_id: str = Field(
            default="",
            description="Optional explicit scheme id (add_scheme); defaults to a "
            "slug of scheme_name.",
        ),
        graph: str = Field(
            default="",
            description="Target graph name for evaluate's engine call (empty = "
            "deployment default).",
        ),
    ) -> str:
        """AIF argument maps: import_aif / export_aif / evaluate / add_scheme."""
        raise not_served("knowledge_graph.argumentation.aif", "EG-FEDERATED-QUERY-R700")

    runtime.REGISTERED_TOOLS["graph_argument"] = graph_argument
    # No bespoke endpoint needed — the generic REST-twin factory in
    # runtime._build_server mounts POST /graph/argument for every
    # ACTION_TOOL_ROUTES entry without a bespoke handler, dispatching through
    # the SAME _execute_tool core.
    runtime.ACTION_TOOL_ROUTES["graph_argument"] = "/graph/argument"
