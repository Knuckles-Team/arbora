"""Shared host admission with controlled EG transport, not durable execution."""

import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbora.api.http.auth import AmbientHTTPAuthenticator
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.registry import Effect, Registry
from arbora.mcp_server import task_plan
from arbora.mcp_server.hosted_invocation import HostedInvocation
from tests.api.test_invoke_contract import caller, operation, setup_services

pytestmark = pytest.mark.spec("ARBORA-HOST-R001.2.1.1")
PROPOSAL = {
    "tasks": [{"task_id": "answer", "prompt": "Answer", "depends_on": []}],
    "output_task_id": "answer",
}


@pytest.fixture
def bridge(monkeypatch):
    run = operation(id="agents.run", scopes=frozenset({"kg:write"}))
    decide = operation(
        id="eg.query.Decide", effect=Effect.READ, scopes=frozenset({"query:decide"})
    )
    services, runtime, *_ = setup_services(run)
    services = replace(services, registry=Registry((run, decide)))
    principal = caller(effective_scopes=frozenset({"kg:write", "query:decide"}))
    from arbora.api.task_plan_schema import TaskPlanSchemaPins

    runtime.schema_published = []
    runtime.schema_digests = {}
    schema_pin = {
        "component_id": "rolling-schema-ace4603da44e4e99929aa926562625fc",
        "kind": "feature_schema",
    }

    async def publish(tenant, pin):
        runtime.schema_published.append((tenant, dict(pin)))
        digest = (
            "sha256:c802a728fe1059505924ebe28533cc588b5f3488c71bb7184ccad3f705b2503f"
            if tenant == "local"
            else "sha256:" + hashlib.sha256(tenant.encode()).hexdigest()
        )
        runtime.schema_digests[tenant] = digest
        return digest

    # The server lifespan publishes every served tenant before requests run;
    # a request only reads the pin and never publishes.
    pins = TaskPlanSchemaPins(schema_pin)
    for tenant in ("tenant:one", "tenant:other", "local"):
        asyncio.run(pins.publish_for(tenant, publish))
    runtime.schema_published = []
    runtime.bindings = {"task_plan_schema_pins": pins}
    monkeypatch.setattr(
        task_plan, "_caller_dispatch_graph", AsyncMock(return_value="verified-graph")
    )
    built = []

    def build(tenant, question, proposal, schema, *, policy):
        built.append((tenant, question, proposal, schema, policy))
        return {"tenant_id": tenant, "question": question, "proposal": proposal}

    send = AsyncMock(return_value=object())
    record = {"caller_principal": "principal:sha256:verified"}
    normalized = {"tasks": PROPOSAL["tasks"], "plan_digest": "engine-digest"}

    def verify(received, request, *, expected_principal):
        assert expected_principal == principal.principal
        if received["caller_principal"] != "principal:sha256:verified":
            raise ValueError("principal mismatch")
        return normalized

    def decode(response):
        return SimpleNamespace(model_dump=lambda **kw: {"records": [record]})

    monkeypatch.setattr(task_plan, "_contract", lambda: (build, verify, send, decode))
    config = task_plan.TaskPlanConfig({}, {"policy": "existing"}, 2, 4)
    host = HostedInvocation(
        services, AmbientHTTPAuthenticator(session_for_request=AsyncMock()), config
    )
    return host, principal, runtime, built, send, record


@pytest.mark.asyncio
async def test_shared_host_uses_own_limits_and_direct_response(bridge):
    host, principal, runtime, built, send, _ = bridge
    result = await host.evaluate_task_plan(principal, "question", PROPOSAL)
    assert built[0][0] == principal.tenant
    assert built[0][2]["max_concurrency"] == 2
    assert built[0][2]["max_steps"] == 4
    assert built[0][3] == {
        "component_id": "rolling-schema-ace4603da44e4e99929aa926562625fc",
        "kind": "feature_schema",
        "definition_digest": runtime.schema_digests[principal.tenant],
    }
    assert built[0][4] == {"policy": "existing"}
    assert runtime.schema_published == []  # the request never publishes
    assert send.await_args.args[2] == "verified-graph"
    assert runtime.verifications >= 6
    assert result["plan_digest"] == "engine-digest"
    with pytest.raises(TypeError):
        result["plan_digest"] = "changed"
    assert runtime.calls == []  # evaluation never dispatches an agent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["tenant", "policy", "max_steps", "max_concurrency", "receipt"]
)
async def test_proposal_cannot_supply_authority_or_limits(bridge, field):
    host, principal, _, _, send, _ = bridge
    with pytest.raises(OperationRefused, match="INVALID_ARGUMENT"):
        await host.evaluate_task_plan(principal, "q", {**PROPOSAL, field: "forged"})
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_policy_and_scope_gate_precedes_engine(bridge):
    host, principal, _, _, send, _ = bridge
    with pytest.raises(OperationRefused, match="SCOPE_REQUIRED"):
        await host.evaluate_task_plan(
            caller(effective_scopes=frozenset()), "q", PROPOSAL
        )
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_revocation_after_engine_call_refuses_content(bridge):
    host, principal, runtime, _, send, _ = bridge

    async def response(*args):
        runtime.on_verify = lambda supplied, count: replace(
            supplied, policy_revision="changed"
        )
        return object()

    send.side_effect = response
    with pytest.raises(OperationRefused, match="UNAUTHENTICATED"):
        await host.evaluate_task_plan(principal, "q", PROPOSAL)


@pytest.mark.asyncio
async def test_response_is_bound_to_verified_principal(bridge):
    host, principal, _, _, _, record = bridge
    record["caller_principal"] = "other"
    with pytest.raises(OperationRefused, match="INVALID_TASK_PLAN_RECEIPT"):
        await host.evaluate_task_plan(principal, "q", PROPOSAL)


@pytest.mark.asyncio
async def test_nested_evaluation_uses_verified_caller_tenant_and_policy(bridge):
    host, _, runtime, built, _, _ = bridge
    principal = caller(
        tenant="tenant:other",
        effective_scopes=frozenset({"kg:write", "query:decide"}),
    )

    await host.evaluate_task_plan(principal, "q", PROPOSAL)

    assert built[0][0] == "tenant:other"
    assert built[0][3]["definition_digest"] == runtime.schema_digests["tenant:other"]
    assert built[0][4] == {"policy": "existing"}
    assert runtime.schema_published == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-RELEASE-R003.4.4")
async def test_nested_evaluation_refuses_unpublished_tenant_without_publishing(
    bridge,
):
    host, _, runtime, _, send, _ = bridge
    principal = caller(
        tenant="tenant:unpublished",
        effective_scopes=frozenset({"kg:write", "query:decide"}),
    )

    with pytest.raises(OperationRefused, match="UNAVAILABLE"):
        await host.evaluate_task_plan(principal, "q", PROPOSAL)
    assert runtime.schema_published == []
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_nested_evaluation_refuses_without_verified_schema_binding(bridge):
    host, principal, runtime, _, send, _ = bridge
    runtime.bindings = {}

    with pytest.raises(OperationRefused, match="UNAVAILABLE"):
        await host.evaluate_task_plan(principal, "q", PROPOSAL)
    send.assert_not_awaited()


@pytest.mark.parametrize("concurrency,steps", [(True, 4), (9, 4), (2, 0), (2, 65)])
def test_host_bounds_are_strict(concurrency, steps):
    with pytest.raises(ValueError):
        task_plan.TaskPlanConfig({}, {}, concurrency, steps)


def test_config_owns_nested_references():
    schema = {"pin": {"revision": 1}}
    config = task_plan.TaskPlanConfig(schema, {}, 1, 1)
    schema["pin"]["revision"] = 2
    assert config.feature_schema["pin"]["revision"] == 1


@pytest.mark.asyncio
async def test_eunomia_denial_never_reaches_engine(bridge):
    from arbora.api.policy.eunomia import PolicyGate
    from tests.a2a.test_operation_bridge import DenyPolicy

    host, principal, _, _, send, _ = bridge
    host = replace(
        host,
        services=replace(
            host.services, policy_gate=PolicyGate("embedded", DenyPolicy())
        ),
    )
    with pytest.raises(OperationRefused, match="POLICY_DENIED"):
        await host.evaluate_task_plan(principal, "q", PROPOSAL)
    send.assert_not_awaited()


def test_missing_eg_contract_requires_upgrade(monkeypatch):
    import medulla.decision_client as contract

    monkeypatch.delattr(contract, "task_plan_request", raising=False)
    with pytest.raises(OperationRefused, match="UPGRADE_REQUIRED"):
        task_plan._contract()


@pytest.mark.asyncio
async def test_unconfigured_host_refuses_without_evaluation(bridge):
    host, principal, _, _, send, _ = bridge
    with pytest.raises(OperationRefused, match="UNAVAILABLE"):
        await replace(host, task_plan_config=None).evaluate_task_plan(
            principal, "q", PROPOSAL
        )
    send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2])
async def test_receipt_requires_exactly_one_record(bridge, monkeypatch, count):
    host, principal, _, _, _, record = bridge
    build, verify, send, _ = task_plan._contract()

    def decode(response):
        return SimpleNamespace(model_dump=lambda **kw: {"records": [record] * count})

    monkeypatch.setattr(task_plan, "_contract", lambda: (build, verify, send, decode))
    with pytest.raises(OperationRefused, match="INVALID_TASK_PLAN_RECEIPT"):
        await host.evaluate_task_plan(principal, "q", PROPOSAL)


@pytest.mark.asyncio
async def test_native_typed_receipt_preserves_premise_wire_aliases(bridge, monkeypatch):
    import json
    from pathlib import Path

    from medulla.decision_client import (
        task_plan_request,
        verify_task_plan_receipt,
    )
    from medulla.generated.models import DecisionBatch

    host, principal, _, _, send, _ = bridge
    batch = DecisionBatch.model_validate_json(
        (Path(__file__).parent / "fixtures/task_plan_native_decision.json").read_text(),
        strict=True,
    )
    record = batch.records[0]
    wire = json.loads(batch.model_dump_json(by_alias=True))["records"][0]
    proposal = wire["inputs"]["params"][0]["value"]["value"]
    principal = caller(
        tenant=record.tenant_id,
        principal="anonymous",
        effective_scopes=principal.effective_scopes,
    )
    config = task_plan.TaskPlanConfig(
        {},
        wire["inputs"]["policy"],
        proposal["max_concurrency"],
        proposal["max_steps"],
    )
    host = replace(host, task_plan_config=config)
    monkeypatch.setattr(
        task_plan,
        "_contract",
        lambda: (task_plan_request, verify_task_plan_receipt, send, lambda _: batch),
    )
    assert record.premises
    result = await host.evaluate_task_plan(
        principal,
        wire["question"]["question_id"],
        {key: proposal[key] for key in ("tasks", "output_task_id")},
    )
    assert result["plan_digest"] == wire["task_plan"]["plan_digest"]
    request = send.await_args.args[1]["request"]
    assert request["tenant_id"] == "local"
    assert request["feature_schema"] == wire["inputs"]["feature_schema"]
    assert request["policy"] == wire["inputs"]["policy"]
    assert host.services.runtime.schema_published == []


@pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")
@pytest.mark.parametrize("value", [True, False, None, 1, "enabled", "ON", " on"])
def test_scaffold_requires_exact_trusted_operator_option(value):
    with pytest.raises(ValueError, match="plan_scaffold"):
        task_plan.TaskPlanConfig({}, {}, 1, 2, plan_scaffold=value)


@pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")
def test_scaffold_defaults_off_and_accepts_explicit_on():
    assert task_plan.TaskPlanConfig({}, {}, 1, 2).plan_scaffold == "off"
    assert (
        task_plan.TaskPlanConfig({}, {}, 1, 2, plan_scaffold="on").plan_scaffold == "on"
    )
