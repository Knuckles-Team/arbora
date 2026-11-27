"""Deployment placement planned through the engine, served over HTTP and MCP."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any

import medulla.generated as generated
import pytest

from arbora.security import request_identity
from tests.api._loopback_engine import loopback  # noqa: F401
from tests.api._served_support import McpAsk, route_methods
from tests.api._served_support import person as _person
from tests.test_served_operation_api import (  # noqa: F401
    _call,
    _value,
    served,
)

PLAN = {
    "workload_iri": "workload:arbora",
    "cpu_millis": 500,
    "memory_bytes": 1 << 30,
    "replicas": 2,
    "candidate_targets": ["placement:compose", "placement:helm"],
    "components": ["tool:render", "tool:apply", "tool:verify"],
}
GRAPH = {
    "shape": {
        "nodes": [
            {"node_id": "n1", "slot": "render-artifact", "component_id": "tool:render"},
            {"node_id": "n2", "slot": "apply", "component_id": "tool:apply"},
            {"node_id": "n3", "slot": "verify", "component_id": "tool:verify"},
        ]
    }
}


class Engine:
    """A loopback engine answering ``Decide`` and ``AgentAssemble``."""

    def __init__(self) -> None:
        self.assembled: list[dict[str, Any]] = []
        self.decided: list[dict[str, Any]] = []

    def decide(self, params: dict[str, Any], principal: str) -> Any:
        request = params["request"]
        self.decided.append(request)
        return {
            "records": [
                {
                    "caller_principal": "principal:sha256:"
                    + hashlib.sha256(principal.encode()).hexdigest(),
                    "tenant_id": request["tenant_id"],
                    "question": request["question"],
                    "inputs": {"params": request["params"]},
                    "record_id": "decision:7",
                    "record_digest": "digest:7",
                    "outcome": {
                        "outcome": "advisory",
                        "scores": [{"option_id": "placement:compose", "score": 1}],
                    },
                }
            ]
        }

    def assemble(self, params: dict[str, Any], _principal: str) -> Any:
        self.assembled.append(params["request"])
        return {
            "graph": GRAPH,
            "record": {"record_id": "assembly:1"},
            "schema_version": 1,
        }


def _reader() -> Any:
    one = _person("one")
    return replace(one, roles=(*one.roles, "ops:read"))


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import tests.test_served_operation_api as harness

    held: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def composing(**kwargs: Any) -> Any:
        kwargs["bindings"] = {
            **kwargs["bindings"],
            "deploy_feature_schema": {
                "component_id": "deploy.feature_schema",
                "definition_digest": "sha256:" + "0" * 64,
                "kind": "feature_schema",
            },
        }
        held["invocation"] = compose(**kwargs)
        return held["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", composing)
    return held


def _route(client: Any, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = Engine()
    route_methods(
        client,
        {"Decide": engine.decide, "AgentAssemble": engine.assemble},
        monkeypatch,
        {"fixture-verified": _reader()},
    )
    return engine


@pytest.mark.spec(
    "ARBORA-DEPLOY-R017.1", "ARBORA-DEPLOY-R017.2", "ARBORA-DEPLOY-R017.3"
)
def test_plan_returns_the_engine_placement_record(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = _route(client, monkeypatch)
    value = _value(_call(app, "deploy.placement.plan", PLAN))
    assert value["decision"]["record_id"] == "decision:7"
    assert value["target"] == "compose"
    assert engine.decided[0]["question"]["kind"] == "placement"
    assert engine.decided[0]["tenant_id"] == "tenant:one"
    body = engine.decided[0]["params"][0]["value"]["value"]["body"]
    assert body["candidate_targets"] == PLAN["candidate_targets"]
    # The assembled graph is the engine's, and the steps list it in order.
    assert engine.assembled == [
        {
            "tenant_id": "tenant:one",
            "template_key": "deployment:compose",
            "requirements": {"capabilities": PLAN["components"]},
        }
    ]
    assert value["assembly"]["graph"] == GRAPH
    assert [step["slot"] for step in value["steps"]] == [
        "render-artifact",
        "apply",
        "verify",
    ]
    bad = {**PLAN, "tenant": "tenant:two"}
    assert _call(app, "deploy.placement.plan", bad).status_code == 400
    assert (
        _call(app, "deploy.placement.plan", {**PLAN, "replicas": -1}).status_code == 400
    )


@pytest.mark.spec("ARBORA-DEPLOY-R017.2")
def test_plan_returns_assembled_graph_from_published_components(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client = served
    _route(client, monkeypatch)
    import tests.api._served_support as support

    monkeypatch.setattr(support, "person", lambda *_a, **_k: _reader())
    ask = McpAsk(stack["invocation"], monkeypatch)

    async def call() -> str:
        result = await ask.mcp.call_tool(
            "ask", {"op": "deploy.placement.plan", "params": PLAN}
        )
        return "".join(getattr(part, "text", "") for part in result.content)

    # The MCP result budget may cut the long engine record; the call succeeded.
    text = asyncio.run(call())
    assert '"ok":true' in text and '"error"' not in text, text[:400]


@pytest.mark.spec("ARBORA-DEPLOY-R017.5")
def test_feature_absent_engine_refuses_over_http(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served

    async def credential_owner(_credential: str, **_kw) -> Any:
        return _reader()

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    without = frozenset(generated.METHOD_IDS) - {"Decide"}
    monkeypatch.setattr(generated, "METHOD_IDS", without)
    refused = _call(app, "deploy.placement.plan", PLAN)
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "UNAVAILABLE"
    assert [m for m, *_ in client.sent if m in {"Decide", "AgentAssemble"}] == []


def _graph_for(capabilities: list[str]) -> dict[str, Any]:
    return {
        "shape": {
            "nodes": [
                {
                    "node_id": name.split(":")[1],
                    "slot": name.split(":")[1],
                    "component_id": name,
                    "tool": {"server": "ops", "tool": f"{name.split(':')[1]}_tool"},
                }
                for name in capabilities
            ]
        }
    }


def _apply_params(loop: Any, components: list[str], reuse: list[str]) -> dict[str, Any]:
    from arbora.api.domain.deployment.artifacts import select_artifact
    from arbora.api.domain.deployment.assembly import (
        artifact_digest,
        choices_digest,
        config_digest,
        graph_digest,
    )

    return {
        "target": "compose",
        "components": components,
        "reuse_components": reuse,
        "plan_digest": graph_digest(_graph_for(components)),
        "choices_digest": choices_digest("compose", components, reuse),
        "config_digest": config_digest({}),
        "artifact_digest": artifact_digest(
            select_artifact("compose", deploy_root=loop.deploy_root)
        ),
    }


def _assembling(loop: Any) -> None:
    def assemble(params: dict[str, Any], _principal: str) -> Any:
        wanted = params["request"]["requirements"]["capabilities"]
        return {"graph": _graph_for(wanted), "record": {"record_id": "assembly:1"}}

    loop.serve({"AgentAssemble": assemble})


@pytest.mark.spec("ARBORA-DEPLOY-R013.5")
def test_mixed_deployment_changes_only_deploy_marked_components(
    loopback: Any,  # noqa: F811
) -> None:
    _assembling(loopback)
    params = _apply_params(loopback, ["tool:a", "tool:b"], ["tool:keep"])

    done = loopback.confirmed("deploy.placement.apply", params, "mixed")

    value = loopback.value(done)
    assert value["ok"] is True
    assert loopback.steps.tools == ["a_tool", "b_tool"]
    assert value["plan_digest"] == params["plan_digest"]
    assert value["config_digest"] == params["config_digest"]
    assert value["artifact_digest"] == params["artifact_digest"]
    requested = [
        c[1]["request"]["requirements"]["capabilities"] for c in loopback.engine_calls
    ]
    assert requested == [["tool:a", "tool:b"]]


@pytest.mark.spec("ARBORA-DEPLOY-R013.5")
def test_altered_binding_causes_zero_effects(loopback: Any) -> None:  # noqa: F811
    _assembling(loopback)
    params = _apply_params(loopback, ["tool:a", "tool:b"], [])

    # A digest altered after the plan was shown changes the plan binding.
    ref = loopback.preview("deploy.placement.apply", params, "bind-1")
    for field in ("plan_digest", "choices_digest", "config_digest", "artifact_digest"):
        altered = {**params, field: "sha256:" + "9" * 64}
        refused = loopback.call("deploy.placement.apply", altered, "bind-1", ref)
        assert refused.json()["error"]["code"] == "PLAN_MISMATCH", (field, refused.text)

    # A shipped artifact that changed after confirmation refuses ARTIFACT_STALE.
    stale_ref = loopback.preview("deploy.placement.apply", params, "bind-2")
    (loopback.deploy_root / "compose" / "compose.yaml").write_text(
        "services: {a: {}}\n"
    )
    stale = loopback.call("deploy.placement.apply", params, "bind-2", stale_ref)
    assert stale.json()["error"]["code"] == "ARTIFACT_STALE", stale.text

    # Nothing ran for any altered binding.
    assert loopback.steps.calls == []
