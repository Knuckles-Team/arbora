"""Present an assembled deployment graph, read only (ARBORA-DEPLOY-R017.3).

A pure function over the engine graph. It holds no reference to a runner and has
no dispatch path: the operator sees the steps, in the engine's order, and
confirms them through plan confirmation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _component(node: Mapping[str, Any]) -> str | None:
    if isinstance(node.get("component_id"), str):
        return node["component_id"]
    kind = node.get("kind")
    if isinstance(kind, Mapping):
        agent = kind.get("agent")
        if isinstance(agent, Mapping):
            return agent.get("agent_id")
        decision = kind.get("decision")
        if isinstance(decision, Mapping):
            dependency = decision.get("decision")
            if isinstance(dependency, Mapping):
                return dependency.get("component_id")
    return None


def render_graph(graph: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The graph's nodes as ``{node_id, slot, component_id, tool}``, engine order."""
    if graph is None:
        return []
    shape = graph.get("shape")
    nodes = (shape if isinstance(shape, Mapping) else graph).get("nodes", ())
    rows: list[dict[str, Any]] = []
    for node in nodes:
        rows.append(
            {
                "node_id": node["node_id"],
                "slot": node.get("slot", node["node_id"]),
                "component_id": _component(node),
                "tool": node.get("tool"),
            }
        )
    return rows
