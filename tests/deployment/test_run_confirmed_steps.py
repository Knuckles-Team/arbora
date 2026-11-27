"""The confirmed step graph runs through governed fleet calls (ARBORA-DEPLOY-R017.21)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.deployment.assembly import graph_digest, run_confirmed_steps
from arbora.security.operation_result import OpError, OpResult

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R017.21")

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
DIGEST = graph_digest(GRAPH)


class Pipeline:
    """The bound invoke pipeline: one recorded outcome per idempotency key."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.recorded: dict[str, Any] = {}
        self.fail_on = fail_on

    async def __call__(self, op: str, params: Any, caller: Any, key: str) -> Any:
        assert caller == "caller"
        if key not in self.recorded:
            self.calls.append((op, dict(params), key))
            failed = self.fail_on is not None and key.endswith(self.fail_on)
            self.recorded[key] = (
                OpError("UPSTREAM_FAILED") if failed else OpResult({"value": 1})
            )
        return self.recorded[key]


def _run(pipeline: Pipeline, **kwargs: Any) -> dict[str, Any]:
    return asyncio.run(
        run_confirmed_steps(
            pipeline,
            "caller",
            GRAPH,
            plan_digest=kwargs.pop("plan_digest", DIGEST),
            **kwargs,
        )
    )


def test_nodes_run_in_engine_order_once_per_key() -> None:
    pipeline = Pipeline()
    first = _run(pipeline, arguments={"apply": {"stack": "arbora"}})
    assert first["ok"] is True
    assert [s["node_id"] for s in first["steps"]] == [
        "render-artifact",
        "apply",
        "verify",
    ]
    assert [c[0] for c in pipeline.calls] == ["fleet.call"] * 3
    assert [c[2] for c in pipeline.calls] == [
        f"deploy:{DIGEST}:{slot}" for slot in ("render-artifact", "apply", "verify")
    ]
    assert pipeline.calls[1][1] == {
        "server": "ops",
        "tool": "apply_tool",
        "arguments": {"stack": "arbora"},
    }
    again = _run(pipeline, arguments={"apply": {"stack": "arbora"}})
    assert again == first and len(pipeline.calls) == 3


def test_reuse_marked_component_step_refuses_before_any_call() -> None:
    pipeline = Pipeline()
    with pytest.raises(OperationRefused) as refused:
        _run(pipeline, reuse=["tool:verify"])
    assert refused.value.code == "REUSE_COMPONENT_MUTATION"
    assert pipeline.calls == []


def test_failed_node_stops_the_run() -> None:
    pipeline = Pipeline(fail_on="apply")
    run = _run(pipeline)
    assert run["ok"] is False
    assert [(s["node_id"], s["ok"]) for s in run["steps"]] == [
        ("render-artifact", True),
        ("apply", False),
    ]
    assert run["steps"][1]["error"] == "UPSTREAM_FAILED"
    assert len(pipeline.calls) == 2


def test_a_changed_graph_refuses_plan_mismatch_and_a_toolless_node_refuses() -> None:
    pipeline = Pipeline()
    with pytest.raises(OperationRefused) as refused:
        _run(pipeline, plan_digest="sha256:" + "0" * 64)
    assert refused.value.code == "PLAN_MISMATCH"
    bare = {"shape": {"nodes": [{"node_id": "n", "slot": "apply"}]}}
    with pytest.raises(OperationRefused) as toolless:
        asyncio.run(
            run_confirmed_steps(
                pipeline, "caller", bare, plan_digest=graph_digest(bare)
            )
        )
    assert toolless.value.code == "INVALID_ARGUMENT"
    assert pipeline.calls == []


class Probe:
    """The read-only reachability and compatibility probe of reused components."""

    def __init__(self, healthy: dict[str, bool]) -> None:
        self.healthy = healthy
        self.probed: list[str] = []

    async def __call__(self, component: str) -> bool:
        self.probed.append(component)
        return self.healthy.get(component, False)


@pytest.mark.spec("ARBORA-DEPLOY-R013.3")
def test_injected_update_for_reused_service_makes_zero_calls() -> None:
    # An update node injected against a reused component refuses before any call,
    # whether or not the probe finds the service healthy.
    graph = {
        "shape": {
            "nodes": [
                {
                    "node_id": "update-engine",
                    "slot": "apply",
                    "component_id": "service:engine",
                    "tool": {"server": "ops", "tool": "update_service"},
                }
            ]
        }
    }
    pipeline = Pipeline()
    probe = Probe({"service:engine": True})
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(
            run_confirmed_steps(
                pipeline,
                "caller",
                graph,
                plan_digest=graph_digest(graph),
                reuse=["service:engine"],
                probe_reused=probe,
            )
        )
    assert refused.value.code == "REUSE_COMPONENT_MUTATION"
    assert pipeline.calls == [] and probe.probed == []
    # An unreachable reused service refuses and never falls back to deploying it.
    unreachable = Probe({})
    with pytest.raises(OperationRefused) as down:
        _run(Pipeline(), reuse=["service:engine"], probe_reused=unreachable)
    assert down.value.code == "REUSE_UNREACHABLE"
    assert down.value.details == {"component_id": "service:engine"}


@pytest.mark.spec("ARBORA-DEPLOY-R013.3")
def test_healthy_reused_service_connects_without_mutation() -> None:
    pipeline = Pipeline()
    probe = Probe({"service:engine": True, "service:cache": True})
    outcome = _run(
        pipeline, reuse=["service:engine", "service:cache"], probe_reused=probe
    )
    assert outcome["ok"] is True
    # Each reused component is probed once, read-only, and no mutation touches it.
    assert sorted(probe.probed) == ["service:cache", "service:engine"]
    assert all(call[0] == "fleet.call" for call in pipeline.calls)
    assert not any("service:" in call[1]["tool"] for call in pipeline.calls)
    assert len(pipeline.calls) == 3
