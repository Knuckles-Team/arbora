"""graph-os publishes its operation registry as versioned data."""

from __future__ import annotations

import pytest

from graph_os.mcp_server import operation_registry as reg
from graph_os.mcp_server.backing.tool_specs import TOOL_SPECS


@pytest.mark.spec("GRAPHOS-HOST-R001.6")
def test_published_document_is_current():
    assert reg.PUBLISHED_PATH.read_text(encoding="utf-8") == reg.render()
    assert reg.main(["--check"]) == 0


@pytest.mark.spec("GRAPHOS-HOST-R001.6")
def test_document_names_exactly_the_served_operations():
    doc = reg.published_operation_registry()
    assert doc["schema_version"] == reg.SCHEMA_VERSION
    assert [op["name"] for op in doc["operations"]] == [s.name for s in TOOL_SPECS]
    body = {
        k: doc[k]
        for k in ("intent_verbs", "operations", "supported_features", "tool_verbs")
    }
    assert doc["registry_digest"] == reg.registry_digest(body)


@pytest.mark.spec("GRAPHOS-HOST-R001.6")
def test_document_has_the_shape_the_harness_reader_consumes(monkeypatch):
    graphos_operations = pytest.importorskip(
        "agent_utilities.skills.graphos_operations"
    )
    monkeypatch.setattr(graphos_operations, "_DATA", reg.PUBLISHED_PATH)
    graphos_operations._load.cache_clear()
    try:
        names = graphos_operations.canonical_operation_names()
        assert {"ask", "find", "act"} <= names
        assert graphos_operations.intent_verbs()
        assert graphos_operations.tool_verbs()
    finally:
        graphos_operations._load.cache_clear()
