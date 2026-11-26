"""An agent promotes an image through the door after an approver approved it (ARBORA-DEPLOY-R017.24)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from test_invoke_contract import FixtureRuntime, run, setup_services

from arbora.api.invoke.plan import EgPlanStore, approval_id_for
from arbora.api.ops import approvals, promote
from arbora.api.registry import Registry
from tests.api._support import make_verified_caller
from tests.api.test_approvals_served import Clock, Leases
from tests.api.test_promote_ops import (
    APPLY,
    DIGEST,
    GRAPH,
    GREEN,
    PARAMS,
    Pipeline,
)

SCOPES = frozenset({"ops:read", "fleet:control"})
PUBLISHING = {
    "gate_class": "publishing",
    "approver_roles": ["release-managers"],
    "expires_in_ms": 60_000,
}


class Runtime(FixtureRuntime):
    """Dispatch to the real promotion handlers over fixture leases and engine."""

    service_scopes = frozenset({"example:execute"})

    def __init__(
        self, leases: Leases, pipeline: Pipeline, records: dict[str, Any]
    ) -> None:
        super().__init__()
        self.leases, self.pipeline, self.records = leases, pipeline, records
        self.handlers = {op.id: op for op in promote.operations()}

    async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
        self.calls.append((op, params, context))
        handler = {
            "deploy.promotions.plan": promote.handle_promotion_plan,
            "deploy.promotions.apply": promote.handle_promotion_apply,
            "deploy.promotions.record": promote.handle_promotion_record,
        }[op.id]
        shaped = SimpleNamespace(
            caller=context.caller,
            client=SimpleNamespace(control_leases=self.leases),
            idempotency_key=context.idempotency_key,
            services={
                "plan_step_invoke": self.pipeline,
                "promotion_ci_tool": {"server": "ci", "tool": "ci_status"},
            },
        )
        return await handler(shaped, params, op)


def _agent() -> Any:
    return make_verified_caller("fleet:control")(
        principal="agent:one", effective_scopes=SCOPES
    )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    import medulla.generated as generated

    leases, clock, records = Leases(), Clock(), {}
    sent: list[str] = []

    async def assemble(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"graph": GRAPH}

    async def engine(_ctx: Any, method: str, params: dict[str, Any], **_k: Any) -> Any:
        sent.append(method)
        if method == "DeployRecordPut":
            records[params["record"]["plan_digest"]] = params["record"]
            return {"plan_digest": params["record"]["plan_digest"], "replayed": False}
        return {"record": records[params["plan_digest"]]}

    monkeypatch.setattr(promote, "_assemble", assemble)
    monkeypatch.setattr(promote, "engine_send", engine)
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | {"DeployRecordPut"}
    )

    async def reader(_op: str, _tenant: str) -> dict[str, Any]:
        return PUBLISHING

    def build(pipeline: Pipeline) -> Any:
        runtime = Runtime(leases, pipeline, records)
        services, *_ = setup_services(
            registry=Registry(list(runtime.handlers.values())),
            runtime=runtime,
            gate_reader=reader,
            external_effect_operations=frozenset({"deploy.promotions.apply"}),
        )
        fields = {f: getattr(services, f) for f in services.__dataclass_fields__}
        fields["plans"] = EgPlanStore(
            SimpleNamespace(control_leases=leases),
            lease_kind="fixture.plan",
            seal_key=b"x" * 32,
            clock_ms=clock,
        )
        return type(services)(**fields), runtime

    return SimpleNamespace(build=build, leases=leases, sent=sent, records=records)


def _approve(leases: Leases, ref: str) -> None:
    item = leases.rows[approval_id_for(ref)]
    approver = SimpleNamespace(
        caller=SimpleNamespace(
            principal="person:rm", tenant="tenant:one", roles=("release-managers",)
        ),
        client=SimpleNamespace(control_leases=leases),
        services={"now_ms": lambda: 5_000_000},
    )
    asyncio.run(
        approvals.handle_approvals_decide(
            approver,
            {
                "approval_id": item["lease_id"],
                "decision": "approved",
                "plan_digest": item["grant"]["request_digest"],
                "reason": "reviewed",
            },
            None,  # type: ignore[arg-type]
        )
    )


# spec: ARBORA-DEPLOY-R017.24
def test_agent_promotes_after_approval_and_refusals_run_nothing(world: Any) -> None:
    pipeline = Pipeline(GREEN)
    services, runtime = world.build(pipeline)

    def go(op_id: str, params: dict[str, Any], **kw: Any) -> Any:
        return run(services, supplied=_agent(), op_id=op_id, params=params, **kw)

    # A tag-only artifact and a red pipeline each run no effect.
    tag = go("deploy.promotions.plan", {**PARAMS, "artifact": "registry.example/app:1"})
    assert tag.code == "IMAGE_NOT_DIGEST_PINNED"
    assert pipeline.calls == []
    red = Pipeline({"status": "failed", "digest": DIGEST})
    red_services, _ = world.build(red)
    refused = run(
        red_services, supplied=_agent(), op_id="deploy.promotions.plan", params=PARAMS
    )
    assert refused.code == "CI_NOT_GREEN"
    assert [t for t, _ in red.calls] == ["ci_status"] and world.sent == []

    # The agent plans, proposes the apply, and an unapproved apply runs nothing.
    plan = go("deploy.promotions.plan", PARAMS)
    assert plan.code == "OK"
    apply_params = {**APPLY, "plan_digest": plan.value["plan_digest"]}
    preview = go("deploy.promotions.apply", apply_params, idempotency_key="k1")
    assert preview.code == "CONFIRMATION_REQUIRED"
    ref = preview.details["plan_ref"]
    before = len(pipeline.calls)
    waiting = go(
        "deploy.promotions.apply", apply_params, idempotency_key="k1", plan_ref=ref
    )
    assert waiting.code == "APPROVAL_REQUIRED"
    assert len(pipeline.calls) == before and world.sent == []

    # An approver approves; the apply runs once and the record is readable.
    _approve(world.leases, ref)
    done = go(
        "deploy.promotions.apply", apply_params, idempotency_key="k1", plan_ref=ref
    )
    assert done.code == "OK" and done.value["ok"] is True
    assert world.sent == ["DeployRecordPut"]
    record = go("deploy.promotions.record", {"plan_digest": plan.value["plan_digest"]})
    assert record.value["record"]["image_digests"] == [DIGEST]
    assert (
        go("deploy.promotions.apply", apply_params, idempotency_key="k1", plan_ref=ref)
        == done
    )
    assert world.sent.count("DeployRecordPut") == 1
