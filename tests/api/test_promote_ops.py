"""Promote a digest-pinned artifact: plan, approval, deploy record (ARBORA-DEPLOY-R017.22, R017.23)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.plan import approval_id_for
from arbora.api.ops import promote
from arbora.deployment.assembly import choices_digest, graph_digest  # noqa: F401
from arbora.security.operation_result import OpResult

DIGEST = "sha256:" + "a" * 64
ARTIFACT = "registry.example/app/api@" + DIGEST
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
PLAN_DIGEST = graph_digest(GRAPH)
PARAMS = {
    "artifact": ARTIFACT,
    "source_env": "staging",
    "target_env": "production",
    "target": "compose",
    "components": [],
}
APPLY = {**PARAMS, "plan_digest": PLAN_DIGEST, "step_arguments": {}}
PLAN_REF = "arbora_plan:" + "1" * 48


class Leases:
    def __init__(self, status: str | None) -> None:
        self.status = status

    async def get(self, *, tenant: str, lease_id: str) -> dict[str, Any] | None:
        assert lease_id == approval_id_for(PLAN_REF)
        return None if self.status is None else {"status": self.status}


class Pipeline:
    """The bound step pipeline: the CI read and the governed fleet calls."""

    def __init__(self, ci: dict[str, Any], fail: str | None = None) -> None:
        self.ci, self.fail = ci, fail
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, op: str, params: Any, caller: Any, key: str) -> Any:
        self.calls.append((params["tool"], key))
        if params["tool"] == "ci_status":
            return OpResult({"value": self.ci})
        if params["tool"] == self.fail:
            from arbora.security.operation_result import OpError

            return OpError("DEPENDENCY_FAILED")
        return OpResult({"value": "done"})


def _context(pipeline: Pipeline, approval: str | None = "consumed") -> Any:
    clock = iter([1_000, 2_000, 3_000])
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant-a", principal="agent:1"),
        client=SimpleNamespace(control_leases=Leases(approval)),
        idempotency_key="plan:" + PLAN_REF,
        services={
            "plan_step_invoke": pipeline,
            "promotion_ci_tool": {"server": "ci", "tool": "ci_status"},
            "now_ms": lambda: next(clock),
        },
    )


@pytest.fixture(autouse=True)
def assembled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def assemble(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"graph": GRAPH}

    monkeypatch.setattr(promote, "_assemble", assemble)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _plan(pipeline: Pipeline, **over: Any) -> Any:
    return _run(
        promote.handle_promotion_plan(_context(pipeline), {**PARAMS, **over}, None)  # type: ignore[arg-type]
    )


GREEN = {"status": "passed", "digest": DIGEST}


# spec: ARBORA-DEPLOY-R017.22
def test_plan_requires_digest_and_green_pipeline() -> None:
    for tag_only in ("registry.example/app/api:1.2", "registry.example/app/api"):
        pipeline = Pipeline(GREEN)
        with pytest.raises(OperationRefused) as refused:
            _plan(pipeline, artifact=tag_only)
        assert refused.value.code == "IMAGE_NOT_DIGEST_PINNED"
        assert pipeline.calls == []  # no connector read before the digest check

    for red in (
        {"status": "failed", "digest": DIGEST},
        {"status": "passed", "digest": "sha256:" + "b" * 64},
        {},
    ):
        with pytest.raises(OperationRefused) as refused:
            _plan(Pipeline(red))
        assert refused.value.code == "CI_NOT_GREEN"

    plan = _plan(Pipeline(GREEN))
    assert plan["plan_digest"] == PLAN_DIGEST and plan["ci"]["status"] == "passed"
    assert [s["node_id"] for s in plan["steps"]] == [
        "render-artifact",
        "apply",
        "verify",
    ]


async def _put(
    sent: list[dict[str, Any]],
    _ctx: Any,
    method: str,
    params: dict[str, Any],
    **_k: Any,
) -> Any:
    assert method == "DeployRecordPut"
    sent.append(params["record"])
    return {"plan_digest": params["record"]["plan_digest"], "replayed": len(sent) > 1}


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import medulla.generated as generated

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"DeployRecordPut", "DeployRecordGet"},
    )

    async def send(ctx: Any, method: str, params: dict[str, Any], **kw: Any) -> Any:
        return await _put(sent, ctx, method, params, **kw)

    monkeypatch.setattr(promote, "engine_send", send)
    return sent


def _apply(pipeline: Pipeline, approval: str | None = "consumed") -> Any:
    from arbora.api.registry import Confirm

    op = next(o for o in promote.operations() if o.id == "deploy.promotions.apply")
    assert op.confirm is Confirm.PLAN
    return _run(promote.handle_promotion_apply(_context(pipeline, approval), APPLY, op))


# spec: ARBORA-DEPLOY-R017.23
def test_apply_needs_approval_records_deploy_and_is_idempotent(
    engine: list[dict[str, Any]],
) -> None:
    # An unapproved or denied plan runs no step and writes no record.
    for approval in (None, "active", "revoked"):
        pipeline = Pipeline(GREEN)
        with pytest.raises(OperationRefused) as refused:
            _apply(pipeline, approval)
        assert refused.value.code == "APPROVAL_REQUIRED"
        assert pipeline.calls == [] and engine == []

    pipeline = Pipeline(GREEN)
    done = _apply(pipeline)
    assert done["ok"] is True
    assert [t for t, _ in pipeline.calls] == [
        "render-artifact_tool",
        "apply_tool",
        "verify_tool",
    ]
    (record,) = engine
    assert record["plan_digest"] == PLAN_DIGEST and record["target"] == "production"
    assert record["image_digests"] == [DIGEST]
    assert record["approver"] == approval_id_for(PLAN_REF)
    assert (record["started_at_ms"], record["finished_at_ms"]) == (1_000, 2_000)
    assert [n["outcome"] for n in record["nodes"]] == ["succeeded"] * 3
    # The same keys run the same steps; the engine answers the record as replayed.
    assert {k for _, k in pipeline.calls} == {
        f"deploy:{PLAN_DIGEST}:{n}" for n in ("render-artifact", "apply", "verify")
    }

    # A failed step records the failure and runs nothing after it.
    engine.clear()
    failing = Pipeline(GREEN, fail="apply_tool")
    failed = _apply(failing)
    assert failed["ok"] is False
    assert [t for t, _ in failing.calls] == ["render-artifact_tool", "apply_tool"]
    assert [(n["node"], n["outcome"]) for n in engine[0]["nodes"]] == [
        ("render-artifact", "succeeded"),
        ("apply", "failed"),
        ("verify", "skipped"),
    ]


# spec: ARBORA-DEPLOY-R017.23
def test_engine_without_the_deploy_record_method_refuses_unavailable_before_any_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {"DeployRecordPut"}
    )
    pipeline = Pipeline(GREEN)
    with pytest.raises(OperationRefused) as refused:
        _apply(pipeline)
    assert refused.value.code == "UNAVAILABLE" and pipeline.calls == []
    assert refused.value.details["missing"] == "DeployRecordPut"


# spec: ARBORA-DEPLOY-R017.24
def test_engine_without_the_deploy_record_read_refuses_unavailable_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {"DeployRecordGet"}
    )
    context = SimpleNamespace(caller=SimpleNamespace(tenant="t"), services={})
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(
            promote.handle_promotion_record(context, {"plan_digest": "d" * 64}, None)  # type: ignore[arg-type]
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "DeployRecordGet"
