"""Policy lifecycle, source binding and the review receipt (ARBORA-OPS-R050, R071)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.audit_evidence import result_evidence
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import plans, policy, policy_review
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Confirm, Effect
from arbora.security.operation_result import OpError, OpResult
from tests.api._lane_double import EngineDouble, context

OPS = {op.id: op for op in (*policy.operations(), *policy_review.operations())}
D = "a" * 64
E = "b" * 64
SOURCE = {
    "repository": "org/constitution",
    "path": "policy/constitution.md",
    "commit": "abcdef1234",
    "blob_digest": D,
}
ACTIVATED = {
    "scope_ref": "policy:org/constitution",
    "change": "create",
    "revision": "r1",
    "candidate_set_digest": E,
    "entities": [{"rule_id": "r1", "clause_digest": D, "entity_id": "node:1"}],
}


def _serve(monkeypatch: pytest.MonkeyPatch, *methods: str) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(policy, "require_models", lambda _names: None)
    monkeypatch.setattr(policy, "require_methods", lambda *_names: None)
    monkeypatch.setattr(policy_review, "require_models", lambda _names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"DecisionLog", *methods},
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)


@pytest.mark.spec("ARBORA-OPS-R050.1")
def test_models_validate_fields_and_closed_change_set() -> None:
    assert {change.value for change in policy.PolicyChange} == {
        "create",
        "update",
        "retire",
    }
    policy.ConstitutionSource(**SOURCE)
    entity = {
        "policy_ref": "p",
        "version_digest": D,
        "constitution_ref": "c",
        "clause_digest": E,
        "state": "active",
    }
    policy.PolicyEntity(**entity)
    for bad in (
        {**entity, "state": "paused"},
        {**entity, "version_digest": "ABC"},
        {**entity, "extra": 1},
    ):
        with pytest.raises(ValidationError):
            policy.PolicyEntity(**bad)
    with pytest.raises(ValidationError):
        policy.ConstitutionSource(**{**SOURCE, "blob_digest": "xyz"})
    from arbora.control_plane.policy.models import PolicyVersion

    assert {"constitution_ref", "state"} <= set(PolicyVersion.model_fields)


@pytest.mark.spec("ARBORA-OPS-R050.2")
@pytest.mark.asyncio
async def test_activation_sends_engine_op_and_returns_entity_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    client = EngineDouble(DecisionLog=ACTIVATED)
    ctx = context(client, monkeypatch)
    params = {"constitution": SOURCE, "receipt_digest": D}
    answer = await policy.handle_policy_change(ctx, params, OPS["policy.create"])
    method, sent, _graph, _key = client.sent[0]
    assert method == "DecisionLog"
    assert sent["op"] == {
        "op": "policy_activation",
        "tenant_id": "tenant-a",
        "change": "create",
        "source": "org/constitution",
        "receipt_digest": D,
    }
    [entity] = answer["entities"]
    assert (entity["clause_digest"], entity["state"]) == (D, "active")
    assert answer["policy_ref"] == "policy:org/constitution"
    again = await policy.handle_policy_change(ctx, params, OPS["policy.create"])
    assert again["entities"] == answer["entities"]
    retired = await policy.handle_policy_change(ctx, params, OPS["policy.retire"])
    assert retired["entities"][0]["state"] == "retired"
    assert client.sent[-1][1]["op"]["change"] == "retire"
    # The engine does not report the op yet: refuse typed UNAVAILABLE, send nothing.
    monkeypatch.undo()
    cold = EngineDouble(DecisionLog=ACTIVATED)
    with pytest.raises(OperationRefused) as unavailable:
        await policy.handle_policy_change(
            context(cold, monkeypatch), params, OPS["policy.create"]
        )
    assert unavailable.value.code == "UNAVAILABLE" and cold.sent == []


@pytest.mark.spec("ARBORA-OPS-R050.3")
@pytest.mark.asyncio
async def test_served_policy_request_reaches_engine_port_and_absent_producer_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = get_registry()
    for op_id in ("policy.create", "policy.update", "policy.retire"):
        op = registry.get(op_id)
        assert op.confirm is Confirm.PLAN and op.effect is Effect.WRITE
        assert op.scopes == {"agent:decision-write"}
        assert op.idempotency.name == "KEY_REQUIRED"
    assert registry.get("policy.review.approve").confirm is Confirm.PLAN
    from arbora.api.invoke.pipeline import _declared_owners

    owners = _declared_owners()
    assert owners["policy.create"] == "medulla:DecisionLog"
    assert owners["policy.review.approve"] == "medulla:DecisionLog"
    # No receipt the engine knows: the engine refuses and arbora writes nothing else.
    _serve(monkeypatch)
    client = EngineDouble(DecisionLog=EngineRefusal("POLICY_REVIEW_MISSING"))
    with pytest.raises(OperationRefused) as refused:
        await policy.handle_policy_change(
            context(client, monkeypatch),
            {"constitution": SOURCE, "receipt_digest": D},
            OPS["policy.create"],
        )
    assert refused.value.code == "POLICY_DENIED"
    assert len(client.sent) == 1


@pytest.mark.spec("ARBORA-OPS-R050.4")
@pytest.mark.asyncio
async def test_three_refusals_have_zero_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    # 1. a change without a verified receipt
    with pytest.raises(OperationRefused) as denied:
        with policy.policy_guard():
            raise EngineRefusal("POLICY_REVIEW_MISSING")
    assert denied.value.code == "POLICY_DENIED"
    # 2. a plan that violates the active policy names the policy
    refusal = EngineRefusal("SCOPE_EXCEEDS_POLICY", details={"policy_ref": "p:1"})
    ran: list[str] = []

    async def violating(*_args: Any, **_kwargs: Any) -> Any:
        raise refusal

    async def schema(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(plans, "decide_task_plan", violating)
    monkeypatch.setattr(plans, "_feature_schema", schema)
    monkeypatch.setattr(
        plans, "_admit_native_plan_run", lambda *_a, **_k: ran.append("admit")
    )
    ctx = SimpleNamespace(
        caller=SimpleNamespace(tenant="t", principal="p", engine_claims={}),
        services={"task_plan_engine": object()},
        client=None,
    )
    with pytest.raises(OperationRefused) as plan_denied:
        await plans.handle_plan_run(
            ctx,
            {"plan": {"tasks": []}},
            get_registry().get("plans.run"),
        )
    assert plan_denied.value.code == "POLICY_DENIED"
    assert plan_denied.value.details["policy_ref"] == "p:1"
    assert ran == []
    # 3. an unreachable engine
    with pytest.raises(OperationRefused) as down:
        with policy.policy_guard():
            raise ConnectionError("engine unreachable")
    assert down.value.code == "POLICY_UNAVAILABLE"
    # An unrelated engine refusal keeps its own code.
    with pytest.raises(EngineRefusal) as other:
        with policy.policy_guard():
            raise EngineRefusal("ACCESS_DENIED")
    assert other.value.code == "ACCESS_DENIED"


@pytest.mark.spec("ARBORA-OPS-R050.5")
def test_audit_record_fields_for_change_and_refused_plan() -> None:
    changed = OpResult(
        value={"policy_ref": "policy:org", "version_digest": E, "entities": []}
    )
    fields = result_evidence("policy.update", {"receipt_digest": D}, changed)
    assert fields == {
        "policy_ref": "policy:org",
        "version_digest": E,
        "approval_ref": D,
        "outcome": "OK",
    }
    refused = OpError("POLICY_DENIED", {"policy_ref": "p:1"})
    plan = result_evidence("plans.run", {"plan": {"tasks": []}}, refused)
    assert plan["policy_ref"] == "p:1" and plan["outcome"] == "POLICY_DENIED"
    assert result_evidence("plans.run", {}, OpResult(value={})) == {}
    for fields_ in (fields, plan):
        assert "clause" not in " ".join(fields_)


@pytest.mark.spec("ARBORA-OPS-R050", "ARBORA-OPS-R050.6")
@pytest.mark.asyncio
async def test_policy_create_then_violating_plan_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    client = EngineDouble(DecisionLog=ACTIVATED)
    created = await policy.handle_policy_change(
        context(client, monkeypatch),
        {"constitution": SOURCE, "receipt_digest": D},
        OPS["policy.create"],
    )
    assert created["entities"]

    async def violating(*_args: Any, **_kwargs: Any) -> Any:
        raise EngineRefusal(
            "SCOPE_EXCEEDS_POLICY", details={"policy_ref": created["policy_ref"]}
        )

    async def schema(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(plans, "decide_task_plan", violating)
    monkeypatch.setattr(plans, "_feature_schema", schema)
    ctx = SimpleNamespace(
        caller=SimpleNamespace(tenant="t", principal="p", engine_claims={}),
        services={"task_plan_engine": object()},
        client=None,
    )
    with pytest.raises(OperationRefused) as refused:
        await plans.handle_plan_run(
            ctx, {"plan": {"tasks": []}}, get_registry().get("plans.run")
        )
    assert refused.value.code == "POLICY_DENIED"
    assert refused.value.details["policy_ref"] == "policy:org/constitution"


BIND = {
    "source_ref": "org/constitution:policy/constitution.md",
    "source_revision": "abcdef1234",
    "source_digest": D,
    "policy_iri": "urn:policy:org",
    "approver": "alice",
    "component_version": "1",
}


@pytest.mark.spec("ARBORA-OPS-R050.2.1")
@pytest.mark.asyncio
async def test_bind_sends_receipt_fields_and_stale_review_leaves_policy_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "PolicySourceBind")
    client = EngineDouble(PolicySourceBind={"bound_at_ms": 5, **BIND})
    answer = await policy.handle_policy_source_bind(
        context(client, monkeypatch), BIND, OPS["policy.source.bind"]
    )
    assert client.sent[0][1] == {"request": BIND}
    assert answer["binding"]["bound_at_ms"] == 5
    for code in (
        "POLICY_SOURCE_DIGEST_MISMATCH",
        "POLICY_SOURCE_STALE",
        "POLICY_SOURCE_UNREVIEWED",
    ):
        from medulla import EngineResponseError

        refusing = EngineDouble(PolicySourceBind=EngineResponseError(code, "no"))
        with pytest.raises(EngineRefusal) as refused:
            await policy.handle_policy_source_bind(
                context(refusing, monkeypatch), BIND, OPS["policy.source.bind"]
            )
        assert refused.value.code == code and len(refusing.sent) == 1
    with pytest.raises(ValidationError):
        policy.PolicySourceBindParams(**{**BIND, "source_digest": "nope"})
    assert OPS["policy.source.bind"].confirm is Confirm.PLAN


REVIEW = {
    "repository": "org/constitution",
    "path": "policy/constitution.md",
    "commit": "abcdef1234",
    "blob_digest": D,
    "candidate_set_digest": E,
    "invariant_generation": "sha256:" + D,
    "expected_policy_revision": None,
    "author": "bob",
}


@pytest.mark.spec("ARBORA-OPS-R071")
@pytest.mark.asyncio
async def test_receipt_binds_every_field_and_refuses_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    client = EngineDouble(DecisionLog={"recorded": "policy_review", "stored": True})
    answer = await policy_review.handle_policy_review_approve(
        context(client, monkeypatch), REVIEW, OPS["policy.review.approve"]
    )
    receipt = answer["receipt"]
    assert receipt["reviewer"] == "alice" and len(receipt["receipt_digest"]) == 64
    write = client.sent[0][1]["op"]["write"]
    assert write["write"] == "record_policy_review"
    binding = write["review"]["binding"]
    assert binding["review_receipt_digest"] == receipt["receipt_digest"]
    for field in (
        "repository",
        "path",
        "commit",
        "blob_digest",
        "candidate_set_digest",
        "invariant_generation",
    ):
        assert binding[field] == REVIEW[field]
    # Any changed bound field changes the digest.
    other = {**REVIEW, "commit": "fedcba9876"}
    again = await policy_review.handle_policy_review_approve(
        context(client, monkeypatch), other, OPS["policy.review.approve"]
    )
    assert again["receipt"]["receipt_digest"] != receipt["receipt_digest"]
    # A review the engine finds stale returns no receipt.
    stale = EngineDouble(DecisionLog=EngineRefusal("POLICY_REVIEW_MISMATCH"))
    with pytest.raises(OperationRefused) as refused:
        await policy_review.handle_policy_review_approve(
            context(stale, monkeypatch), REVIEW, OPS["policy.review.approve"]
        )
    assert refused.value.code == "PLAN_STALE"
    # A rejected review is never recorded; an author cannot review their own file.
    rejected = EngineDouble(DecisionLog={})
    none = await policy_review.handle_policy_review_approve(
        context(rejected, monkeypatch),
        {**REVIEW, "approved": False},
        OPS["policy.review.approve"],
    )
    assert none["receipt"] is None and rejected.sent == []
    with pytest.raises(OperationRefused) as self_review:
        await policy_review.handle_policy_review_approve(
            context(rejected, monkeypatch),
            {**REVIEW, "author": "alice"},
            OPS["policy.review.approve"],
        )
    assert self_review.value.code == "POLICY_DENIED" and rejected.sent == []


@pytest.mark.spec("ARBORA-OPS-R071")
def test_review_receipt_has_one_producer() -> None:
    registry = get_registry()
    assert registry.get("policy.review.approve") is not None
    assert registry.get("policy.review") is None
    assert not hasattr(policy, "handle_policy_review")
    assert "policy.review" not in policy.NATIVE_IDEMPOTENCY
