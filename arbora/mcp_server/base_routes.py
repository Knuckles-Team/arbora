"""The base action-routed REST twins every host tool table starts with.

A host tool table copies this seed at construction; each registrar then adds its own
twins through ``ToolTable.bind_route``. The seed itself never changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = ["BASE_ACTION_TOOL_ROUTES"]

# Single source of truth: every action-routed MCP tool in the host tool table
# has exactly one collapsed action-routed REST twin (POST, JSON body carries the
# ``action`` and its args). Granular CRUD sub-routes (``/graph/write/node`` etc.)
# are layered on top for fine-grained HTTP clients, but this map guarantees that
# anything callable over MCP is also callable over REST and vice versa. The
# parity contract test (tests/unit/test_gateway_mcp_parity.py) asserts this map
# stays in lockstep with the host tool table so the two surfaces never drift.
BASE_ACTION_TOOL_ROUTES: Mapping[str, str] = MappingProxyType(
    {
        "graph_query": "/graph/query",
        "tabular_query": "/query/tabular",
        "graph_ask": "/graph/ask",
        "graph_table": "/graph/table",
        "graph_search": "/graph/search",
        "graph_search_synthesis": "/graph/search-synthesis",
        "graph_code_nav": "/graph/code-nav",
        "graph_document_tree": "/graph/document-tree",
        "graph_write": "/graph/write",
        "graph_ingest": "/graph/ingest",
        "graph_analyze": "/graph/analyze",
        "graph_code": "/graph/code",
        "graph_evaluate": "/graph/evaluate",
        "graph_explain": "/graph/explain",
        "graph_config": "/graph/config",
        "graph_configure": "/graph/configure",
        "graph_context": "/graph/context",
        "graph_feedback": "/graph/feedback",
        "graph_sessions": "/graph/sessions",
        "graph_reach": "/graph/reach",
        "graph_secret": "/graph/secret",  # nosec B105 - a route path, not a secret
        "document_process": "/document/process",
        "source_connector": "/connector/source",
        "graph_writeback": "/graph/writeback",
        "spec_ticket": "/spec/ticket",
        "source_sync": "/source/sync",
        "source_drain": "/source/drain",
        "graph_etl": "/graph/etl",
        "ontology_property_types": "/ontology/property-types",
        "ontology_value_types": "/ontology/value-types",
        "ontology_interface": "/ontology/interface",
        "ontology_function": "/ontology/function",
        "ontology_derive": "/ontology/derive",
        "ontology_link_materialize": "/ontology/link-materialize",
        "ontology_leanix_sync": "/ontology/leanix-sync",
        "ontology_classification_claims": "/ontology/classification-claims",
        "ontology_repository_provenance": "/ontology/repository-provenance",
        "graph_ontology": "/graph/ontology",
        "object_edits": "/object/edits",
        "object_index": "/object/index",
        "object_permissioning": "/object/permissioning",
        "object_set": "/object/set",
        "graph_share": "/graph/share",
        "graph_feeds": "/graph/feeds",
        "graph_candidate_claims": "/graph/candidate-claims",
        "skill_classify": "/skill/classify",
        "browser_control": "/browser/control",
        "graph_a2a": "/graph/a2a",
    }
)
