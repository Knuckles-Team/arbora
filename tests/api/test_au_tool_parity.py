"""Served engine operations that replace the in-run agent-utilities tool wrappers."""

from __future__ import annotations

import pytest

from arbora.api.ops import query
from arbora.api.registry import EgMethod
from arbora.api.registry.eg_binding import load_eg_bindings
from arbora.api.registry.spec import Effect
from tests.api._catalog import catalog, method_row, scope_row

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
    # Curated query ops bind their engine methods, so the method is served
    # under its curated id; key it by the engine method id here.
    curated = query.operations()
    served = {op.id: op for op in load_eg_bindings(curated_ops=curated)}
    served.update(
        {
            f"eg.query.{op.binding.service}": op
            for op in curated
            if isinstance(op.binding, EgMethod)
        }
    )
    return served


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


@pytest.mark.spec("ARBORA-OPS-R843.2")
def test_media_transcription_is_served_by_the_engine_asr_method(tmp_path) -> None:
    """An engine whose catalog lists ``Asr`` serves it as ``eg.ingestion.Asr``.

    The default engine build omits the transcription feature, so its catalog
    carries no ``Asr`` row; the disposition binds whenever the catalog does.
    """
    document = catalog(
        (method_row("Asr", "asr:transcribe", domain="ingestion", idempotent=False),),
        (scope_row("asr:transcribe", "user"),),
    )
    # The fixture catalog carries only Asr, so the production exclusions would rot.
    no_exclusions = tmp_path / "exclusions.yaml"
    no_exclusions.write_text("exclusions: []\n", encoding="utf-8")
    served = {
        op.id: op
        for op in load_eg_bindings(catalog=document, exclusions_path=no_exclusions)
    }
    assert served["eg.ingestion.Asr"].effect is not None
