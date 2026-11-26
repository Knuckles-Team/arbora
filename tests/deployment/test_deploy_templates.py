"""Deployment templates and tool components are published and resolve (ARBORA-DEPLOY-R601)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest

pytest.importorskip("medulla.generated.models")

from arbora.deployment import assembly_library as library  # noqa: E402
from arbora.deployment.assembly import assemble_deployment  # noqa: E402
from tests.deployment.test_assembly_library import (  # noqa: E402
    LoopbackEngine,
    _bindings,
    _context,
)

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R601")


def _assemble_from(engine: LoopbackEngine, tenant: str):
    """The engine's resolution of ``template_key``: the published head of that id.

    The answer is solved only when every dependency the graph names resolves to a
    published component head of the same definition digest; otherwise the engine
    abstains naming the first dependency it could not resolve.
    """

    async def send(body: Mapping[str, Any]) -> Mapping[str, Any]:
        key = body["request"]["template_key"]
        graph = engine.heads.get(("AgentGraph", tenant, key))
        if graph is None:
            return {"graph": None, "record": {"outcome": {"abstained": "no_template"}}}
        for node in graph["shape"]["nodes"]:
            template = node["kind"]
            if template == "end":
                continue
            pin = template["template"]["bindings"]["tool"]
            head = engine.heads.get(("AgentComponent", tenant, pin["component_id"]))
            if head is None or head["definition_digest"] != pin["definition_digest"]:
                return {
                    "graph": None,
                    "record": {
                        "outcome": {"abstained": f"unresolved:{pin['component_id']}"}
                    },
                }
        return {"graph": graph, "record": {"outcome": "solved"}}

    return send


def test_templates_published_and_assemble_solved() -> None:
    engine = LoopbackEngine()
    asyncio.run(
        library.publish_at_startup(
            engine.send, _context, ["tenant:one"], _bindings(), attempts=1
        )
    )
    send = _assemble_from(engine, "tenant:one")
    capabilities = [f"deployment:tool:{role}:in_process" for role in library.ROLES]

    for target in library.TARGETS:
        answer = asyncio.run(
            assemble_deployment(
                send, tenant="tenant:one", target=target, components=capabilities
            )
        )
        assert answer["record"] == {"outcome": "solved"}
        graph = answer["graph"]
        assert graph["graph_id"] == f"deployment:{target}"
        slots = [n["node_id"] for n in graph["shape"]["nodes"]]
        assert slots == ["render-artifact", "apply", "verify", "done"]

    # A target whose tool slot is unconfigured publishes no graph, so it abstains.
    bare = LoopbackEngine()
    partial = _bindings(skip=("apply", "helm"))
    results = asyncio.run(
        library.publish_deployment_library(bare.send, _context, "tenant:one", partial)
    )
    assert results["helm"] is False and results["in_process"] is True
    missing = asyncio.run(
        assemble_deployment(
            _assemble_from(bare, "tenant:one"),
            tenant="tenant:one",
            target="helm",
            components=[],
        )
    )
    assert missing["graph"] is None
