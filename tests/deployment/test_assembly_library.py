"""The deployment template library is published once per tenant (ARBORA-DEPLOY-R017.20)."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
from typing import Any

import pytest

pytest.importorskip("medulla.generated.models")
from medulla.generated import models  # noqa: E402

from arbora.deployment import assembly_library as library  # noqa: E402

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R017.20")

_PARAMS: dict[str, Any] = {
    "AgentComponent": models.MethodAgentComponentParams,
    "AgentTemplate": models.MethodAgentTemplateParams,
    "AgentGraph": models.MethodAgentGraphParams,
}
_HEAD = {
    "AgentComponent": ("component_id", "component"),
    "AgentTemplate": ("template_id", "template"),
    "AgentGraph": ("graph_id", "graph"),
}


_COUNTER = itertools.count(1)


class LoopbackEngine:
    """An engine library: validates each request against the engine's own types."""

    def __init__(self) -> None:
        self.heads: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.writes: list[tuple[str, str, Any]] = []

    async def send(self, method: str, params: dict[str, Any], key: str | None) -> Any:
        _PARAMS[method].model_validate(params)
        op = params["op"]
        id_field, field = _HEAD[method]
        if op["op"] == "current":
            return self.heads.get((method, op["tenant_id"], op[id_field]))
        request = op["request"]
        draft = request[field]
        context = request["context"]
        assert draft["tenant_id"] == context["tenant_id"]
        ident = draft[id_field]
        head = self.heads.get((method, draft["tenant_id"], ident))
        assert context["expected_revision"] == (
            0 if head is None else head["entry_revision"]
        )
        revision = context["expected_revision"] + 1
        digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(draft, sort_keys=True, default=str).encode()
            ).hexdigest()
        )
        entry = {**draft, "entry_revision": revision, "definition_digest": digest}
        self.heads[(method, draft["tenant_id"], ident)] = entry
        self.writes.append((method, ident, key and revision))
        return {field: {"definition_digest": digest}}


async def _context(tenant: str, key: str) -> dict[str, Any]:
    return {
        "request_id": next(_COUNTER),
        "principal": "principal:sha256:" + "a" * 64,
        "caller_principal": "principal:sha256:" + "b" * 64,
        "attempt_nonce": f"{next(_COUNTER):064x}",
        "actor_scope": "action-scope:arbora",
        "purpose_id": library.PURPOSE_ID,
        "policy_revision": "policy-v1",
        "policy_digest": "sha256:" + "7" * 64,
        "policy_decision_id": "decision:1",
        "tenant_id": tenant,
        "idempotency_key": key,
        "expected_revision": None,
        "trace_id": None,
        "created_at_ms": 1,
    }


def _bindings(skip: tuple[str, str] | None = None) -> dict[tuple[str, str], Any]:
    return {
        (role, target): library.ToolBinding(f"{target}-mcp", f"{role}_tool")
        for role in library.ROLES
        for target in library.TARGETS
        if (role, target) != skip
    }


def test_startup_publishes_templates_and_components_once_per_tenant() -> None:
    engine = LoopbackEngine()
    results = asyncio.run(
        library.publish_at_startup(
            engine.send, _context, ["tenant-a", "tenant-b", "tenant-a"], _bindings()
        )
    )
    assert results == {
        "tenant-a": dict.fromkeys(library.TARGETS, True),
        "tenant-b": dict.fromkeys(library.TARGETS, True),
    }
    for tenant in ("tenant-a", "tenant-b"):
        for target in library.TARGETS:
            graph = engine.heads[("AgentGraph", tenant, f"deployment:{target}")]
            nodes = graph["shape"]["nodes"]
            assert [n["node_id"] for n in nodes] == [*library.ROLES, "done"]
            bound = [n["kind"]["template"]["bindings"]["tool"] for n in nodes[:3]]
            assert [b["component_id"] for b in bound] == [
                f"deployment:tool:{role}:{target}" for role in library.ROLES
            ]
        tools = engine.heads[("AgentComponent", tenant, "deployment:tool:apply:helm")]
        assert tools["attributes"] == {"server": "helm-mcp", "tool": "apply_tool"}
        assert tools["facts"]["effect"] == "write"
    first_writes = len(engine.writes)
    assert first_writes == 2 * (5 + 12 + 1 + 4)

    again = asyncio.run(
        library.publish_at_startup(engine.send, _context, ["tenant-a"], _bindings())
    )
    assert again["tenant-a"] == dict.fromkeys(library.TARGETS, True)
    assert len(engine.writes) == first_writes


def test_a_changed_tool_publishes_a_new_revision_against_the_head() -> None:
    engine = LoopbackEngine()
    asyncio.run(
        library.publish_deployment_library(engine.send, _context, "t", _bindings())
    )
    changed = _bindings()
    changed[("apply", "compose")] = library.ToolBinding("other-mcp", "apply_tool")
    asyncio.run(library.publish_deployment_library(engine.send, _context, "t", changed))
    head = engine.heads[("AgentComponent", "t", "deployment:tool:apply:compose")]
    assert head["entry_revision"] == 2
    assert head["attributes"]["server"] == "other-mcp"


def test_unconfigured_tool_leaves_target_uncovered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = LoopbackEngine()
    with caplog.at_level(logging.WARNING, logger=library.logger.name):
        result = asyncio.run(
            library.publish_deployment_library(
                engine.send, _context, "t", _bindings(skip=("verify", "swarm"))
            )
        )
    assert result["swarm"] is False
    assert all(result[t] for t in ("in_process", "compose", "helm"))
    assert ("AgentGraph", "t", "deployment:swarm") not in engine.heads
    assert not any(
        key[2].endswith(":swarm") and key[0] == "AgentComponent" for key in engine.heads
    )
    assert "DEPLOY_TOOL_UNCONFIGURED: no tool for verify on swarm" in caplog.text


def test_no_configuration_publishes_nothing() -> None:
    engine = LoopbackEngine()
    result = asyncio.run(
        library.publish_deployment_library(engine.send, _context, "t", {})
    )
    assert not any(result.values()) and engine.writes == []


def test_a_failing_tenant_is_retried_then_logged_and_startup_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = LoopbackEngine()
    delays: list[float] = []
    calls = 0

    async def flaky(method: str, params: dict[str, Any], key: str | None) -> Any:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("engine starting")
        return await engine.send(method, params, key)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = asyncio.run(
        library.publish_at_startup(flaky, _context, ["t"], _bindings(), sleep=sleep)
    )
    assert all(result["t"].values()) and delays == [0.5, 1.0]

    async def down(*_args: Any) -> Any:
        raise ConnectionError("down")

    with caplog.at_level(logging.ERROR, logger=library.logger.name):
        gone = asyncio.run(
            library.publish_at_startup(
                down, _context, ["t"], _bindings(), attempts=2, sleep=sleep
            )
        )
    assert not any(gone["t"].values())
    assert "deployment library not published for t after 2 attempts" in caplog.text


def test_tool_bindings_read_the_role_target_matrix() -> None:
    document = {
        "tools": {
            "apply": {
                "compose": {"server": "c", "tool": "up"},
                "helm": {"server": "h"},
            },
            "bogus": {"compose": {"server": "x", "tool": "y"}},
        }
    }
    assert library.tool_bindings(document) == {
        ("apply", "compose"): library.ToolBinding("c", "up")
    }
    assert library.tool_bindings(None) == {}
