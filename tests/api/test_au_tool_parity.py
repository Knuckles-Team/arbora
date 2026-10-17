"""Served engine operations that replace the in-run agent-utilities tool wrappers."""

from __future__ import annotations

import pytest

from graph_os.api.registry.eg_binding import load_eg_bindings
from graph_os.api.registry.spec import Effect

# Each replaced wrapper module maps to the engine methods that deliver its
# behaviour, and to the subset that must stay read-only for the read tools.
_DELIVERED = {
    "kg_tools": (
        ("eg.query.UnifiedQuery", "eg.ingestion.SemanticSearch", "eg.query.Uql"),
        True,
    ),
    "knowledge_tools_read": (
        (
            "eg.query.UnifiedQuery",
            "eg.ingestion.SemanticSearch",
            "eg.graph.GetSubgraph",
        ),
        True,
    ),
    "knowledge_tools_write": (
        ("eg.graph.AddNode", "eg.graph.AddEdge", "eg.graph.CompareAndSetNodeFields"),
        False,
    ),
    "code_intelligence_tools": (
        ("eg.compute.GetBlastRadius", "eg.query.UnifiedQuery", "eg.graph.GetNeighbors"),
        True,
    ),
    "code_index": (("eg.ingestion.IndexRepository",), False),
}


@pytest.fixture(scope="module")
def served() -> dict:
    return {op.id: op for op in load_eg_bindings()}


@pytest.mark.spec("ARBORA-OPS-R843.1")
@pytest.mark.parametrize("module", sorted(_DELIVERED))
def test_replaced_wrapper_methods_are_served(served: dict, module: str) -> None:
    """Every method a replaced wrapper needs is a generated operation; reads stay reads."""
    methods, read_only = _DELIVERED[module]
    for method in methods:
        assert method in served, f"{module}: {method} is not served"
        if read_only:
            assert served[method].effect is Effect.READ, f"{module}: {method} mutates"
        else:
            assert served[method].effect is not None
