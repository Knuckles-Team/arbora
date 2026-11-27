"""The assembled graph is shown read only, in the engine's order."""

from __future__ import annotations

import pytest

from arbora.api.domain.deployment import assembly_view
from arbora.api.domain.deployment.assembly_view import render_graph

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R017.3")


def test_nodes_listed_in_engine_order_and_no_dispatch_path() -> None:
    graph = {
        "shape": {
            "nodes": [
                {"node_id": "n3", "slot": "verify", "component_id": "c3", "tool": "t3"},
                {
                    "node_id": "n1",
                    "slot": "render-artifact",
                    "kind": {"agent": {"agent_id": "c1", "definition_digest": "d"}},
                },
                {"node_id": "n2", "slot": "apply", "component_id": "c2"},
            ]
        }
    }
    assert render_graph(graph) == [
        {"node_id": "n3", "slot": "verify", "component_id": "c3", "tool": "t3"},
        {
            "node_id": "n1",
            "slot": "render-artifact",
            "component_id": "c1",
            "tool": None,
        },
        {"node_id": "n2", "slot": "apply", "component_id": "c2", "tool": None},
    ]
    assert render_graph(None) == []
    source = open(assembly_view.__file__, encoding="utf-8").read()
    assert "import arbora" not in source and "from arbora" not in source
    assert "run_confirmed_steps" not in source
