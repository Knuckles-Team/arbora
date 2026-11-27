"""The confirmed deployment apply, served over HTTP (ARBORA-DEPLOY-R017.4, R017.19, R017.21)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from arbora.deployment.artifacts import select_artifact
from arbora.deployment.assembly import (
    artifact_digest,
    choices_digest,
    config_digest,
    graph_digest,
)
from arbora.security.operation_result import OpResult
from tests.api._served_support import confirmable_invocation, person, route_methods
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

KEY = "Idempotency-Key"
PLAN = "Arbora-Plan-Ref"
GRAPH = {
    "shape": {
        "nodes": [
            {
                "node_id": slot,
                "slot": slot,
                "component_id": f"tool:{slot}",
                "tool": {"server": "ops", "tool": f"{slot}_tool"},
            }
            for slot in ("render-artifact", "apply", "verify")
        ]
    }
}
COMPONENTS = ["tool:render-artifact", "tool:apply", "tool:verify"]
APPLY = {
    "target": "compose",
    "components": COMPONENTS,
    "plan_digest": graph_digest(GRAPH),
    "choices_digest": choices_digest("compose", COMPONENTS, ()),
    "config_digest": config_digest({}),
    "artifact_digest": artifact_digest(select_artifact("compose")),
}


class StepPipeline:
    """The bound step pipeline: records the governed fleet calls of the run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, op: str, params: Any, caller: Any, key: str) -> Any:
        self.calls.append((params["tool"], key))
        return OpResult({"value": "done"})


class Engine:
    def __init__(self) -> None:
        self.assembled = 0

    def assemble(self, params: dict[str, Any], _principal: str) -> Any:
        self.assembled += 1
        return {"graph": GRAPH, "record": {"record_id": "assembly:1"}}


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> StepPipeline:
    import tests.test_served_operation_api as harness

    steps = StepPipeline()
    compose = harness.compose_served_invocation

    def composing(**kwargs: Any) -> Any:
        kwargs["bindings"] = {
            **kwargs["bindings"],
            "plan_step_invoke": steps,
            "deploy_config": {},
        }
        return compose(**kwargs)

    monkeypatch.setattr(harness, "compose_served_invocation", composing)
    confirmable_invocation(monkeypatch)
    return steps


@pytest.fixture
def env(
    stack: StepPipeline,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    steps = stack
    app, client = served
    engine = Engine()
    one = person("one")
    route_methods(
        client,
        {"AgentAssemble": engine.assemble},
        monkeypatch,
        {"fixture-verified": replace(one, roles=(*one.roles, "fleet:control"))},
    )
    return app, engine, steps


def _preview(app: Any, params: dict[str, Any], key: str) -> str:
    preview = _call(app, "deploy.placement.apply", params, **{KEY: key})
    assert preview.json()["error"]["code"] == "CONFIRMATION_REQUIRED", preview.text
    return preview.json()["error"]["details"]["plan_ref"]


@pytest.mark.spec(
    "ARBORA-DEPLOY-R017.4", "ARBORA-DEPLOY-R017.19", "ARBORA-DEPLOY-R017.21"
)
def test_plan_then_confirmed_apply_runs_once(env: Any) -> None:
    app, engine, steps = env
    ref = _preview(app, APPLY, "k1")
    assert steps.calls == [] and engine.assembled == 0
    done = _call(app, "deploy.placement.apply", APPLY, **{KEY: "k1", PLAN: ref})
    assert done.status_code == 200, done.text
    value = _value(done)
    assert value["ok"] is True
    assert [s["node_id"] for s in value["steps"]] == [
        "render-artifact",
        "apply",
        "verify",
    ]
    assert [t for t, _ in steps.calls] == [
        "render-artifact_tool",
        "apply_tool",
        "verify_tool",
    ]
    replay = _call(app, "deploy.placement.apply", APPLY, **{KEY: "k1", PLAN: ref})
    assert replay.status_code in {200, 400, 409} and len(steps.calls) == 3


@pytest.mark.spec("ARBORA-DEPLOY-R017.4", "ARBORA-DEPLOY-R017.19")
def test_apply_without_confirmed_plan_refuses_and_runs_nothing(env: Any) -> None:
    app, engine, steps = env
    ref = _preview(app, APPLY, "k2")
    for headers in ({KEY: "k2"}, {KEY: "k2", PLAN: "plan:forged"}):
        refused = _call(app, "deploy.placement.apply", APPLY, **headers)
        assert refused.status_code >= 400
    assert steps.calls == [] and engine.assembled == 0
    assert ref


@pytest.mark.spec(
    "ARBORA-DEPLOY-R017.4", "ARBORA-DEPLOY-R017.19", "ARBORA-DEPLOY-R017.21"
)
def test_changed_digest_refuses_plan_mismatch(env: Any) -> None:
    app, engine, steps = env
    ref = _preview(app, APPLY, "k3")
    changed = {**APPLY, "artifact_digest": "sha256:" + "b" * 64}
    refused = _call(app, "deploy.placement.apply", changed, **{KEY: "k3", PLAN: ref})
    assert refused.json()["error"]["code"] == "PLAN_MISMATCH", refused.text
    assert steps.calls == []
    stale = {**APPLY, "plan_digest": "sha256:" + "0" * 64}
    stale_ref = _preview(app, stale, "k4")
    refused = _call(
        app, "deploy.placement.apply", stale, **{KEY: "k4", PLAN: stale_ref}
    )
    assert refused.json()["error"]["code"] == "PLAN_MISMATCH", refused.text
    assert steps.calls == []
