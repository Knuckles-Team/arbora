"""Invoked AU chain with a real model adapter and controlled EG transport."""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from meristem import host_ports
from meristem.graph import RunRequest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from arbora import agent_runs
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
from arbora.api.contracts.session import GraphSession
from arbora.api.ops import plans
from arbora.api.registry import Effect, Registry
from arbora.mcp_server import task_plan, task_plan_runtime
from arbora.security.actor_context import ActorContext, ActorType
from tests.api.test_invoke_contract import operation
from tests.mcp_server import test_task_plan as fixtures
from tests.test_native_run_scope import CLAIM, SESSION

PROPOSAL = fixtures.PROPOSAL
bridge = fixtures.bridge

pytestmark = pytest.mark.spec("ARBORA-HOST-R001.2.1.2")


@pytest.fixture
def chain(bridge, monkeypatch):
    host, principal, runtime, built, send, record = bridge
    config = replace(host.task_plan_config, request_limit=3, total_tokens_limit=10000)
    # The publication operations go through the same host policy gates.
    registry = Registry(
        tuple(
            host.services.registry.get(name)
            for name in ("agents.run", "eg.query.Decide")
        )
        + tuple(
            operation(id=name, effect=Effect.READ, scopes=frozenset({"kg:write"}))
            for name in (
                "eg.coordination.PublishCurrentPlan",
                "eg.coordination.GetCurrentPlan",
            )
        )
    )
    host = replace(
        host,
        task_plan_config=config,
        services=replace(host.services, registry=registry),
    )
    canonical = {
        **PROPOSAL,
        "max_concurrency": 2,
        "max_steps": 4,
        "plan_digest": "digest",
        "input_digest": "input",
        "policy_digest": "policy",
        "policy_revision": "1",
        "total_tasks": 1,
    }
    original = task_plan._contract()
    monkeypatch.setattr(
        task_plan,
        "_contract",
        lambda: (original[0], lambda *a, **kw: canonical, original[2], original[3]),
    )
    monkeypatch.setattr(
        task_plan_runtime, "caller_from_session", lambda *a, **k: principal
    )
    monkeypatch.setattr(
        task_plan_runtime, "_caller_dispatch_graph", AsyncMock(return_value="graph")
    )
    model_calls = []

    def model(messages, info):
        model_calls.append((messages, info))
        assert not info.function_tools and not info.output_tools
        return ModelResponse(
            parts=[TextPart(json.dumps(PROPOSAL) if len(model_calls) == 1 else "done")],
            usage=RequestUsage(input_tokens=7, output_tokens=3),
        )

    model = FunctionModel(model)
    monkeypatch.setattr(agent_runs, "resolve_model", lambda name: model)
    monkeypatch.setattr(agent_runs, "run_config", lambda: {"agent_model": "test"})
    state = {"snapshot": None, "published": []}

    async def publish(client, params, graph):
        assert graph == "owner-graph"
        state["published"].append(params["request"])
        state["snapshot"] = params["request"]["snapshot"]
        return SimpleNamespace(run_id="run", revision=state["snapshot"]["revision"])

    async def read(*args):
        assert args[2] == "owner-graph"
        return (
            None
            if state["snapshot"] is None
            else SimpleNamespace(model_dump=lambda **k: state["snapshot"])
        )

    eg = SimpleNamespace(
        send_publish_current_plan=publish,
        decode_publish_current_plan=lambda x: x,
        send_get_current_plan=read,
        decode_get_current_plan=lambda x: x,
    )
    import meristem.graph as au

    monkeypatch.setattr(task_plan_runtime, "_contracts", lambda: (au, eg))
    host_ports.register_run_decision_provider(agent_runs.decide_run)
    host_ports.register_model_provider(lambda name: model)
    yield host, state, model_calls
    host_ports.register_run_decision_provider(None)
    host_ports.register_model_provider(None)


@pytest.mark.asyncio
async def test_registered_provider_executes_only_durable_plan(chain):
    host, state, calls = chain
    events = []
    host_ports.register_run_event_sink(events.append)
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="different-task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        result = await agent_runs.AgentRunPort().execute_agent(
            AgentExecutionRequest(
                agent_name="helper", task="actual user question", run_id="run"
            ),
            session=SESSION,
        )
    assert result.output == "done"
    assert result.usage == {
        "requests": 2,
        "tool_calls": 0,
        "input_tokens": 14,
        "output_tokens": 6,
        "total_tokens": 20,
    }
    assert events[-1]["result"]["usage"] == result.usage
    host_ports.register_run_event_sink(None)
    assert len(calls) == 2
    payload = state["published"][0]
    assert payload["snapshot"]["revision"] == 0
    assert payload["snapshot"]["run_id"] == "run"
    assert payload["task_plan_proposal"]["tasks"] == PROPOSAL["tasks"]
    assert payload["snapshot"]["task_plan"]["plan_digest"] == "digest"
    assert payload["fencing_token"] == CLAIM["fencing_token"]
    assert "actual user question" in repr(calls[0][0])
    assert "fencing_token" not in repr(calls)


@pytest.mark.asyncio
async def test_native_plan_helper_wires_real_publication_and_owner_scopes(
    chain, monkeypatch
):
    """Exercise the served helper through canonical publication and scopes."""
    host, state, _ = chain
    principal = fixtures.caller(
        effective_scopes=frozenset({"kg:write", "query:decide"})
    )
    session = GraphSession(
        actor=ActorContext(
            actor_id=principal.principal,
            actor_type=ActorType.HUMAN,
            tenant_id=principal.tenant,
            authenticated=True,
        ),
        tenant=principal.tenant,
        graph="owner-graph",
        scopes=principal.effective_scopes,
        audience="arbora",
        policy_version=principal.policy_revision,
    )
    caller = replace(principal, request_id="request:native-helper", session=session)
    claim = {
        **CLAIM,
        "tenant": session.tenant,
        "work_item_id": "work",
        "lease_expires_at_ms": 2**63 - 1,
    }
    admission = plans._NativePlanAdmission(
        item_id="work",
        run_id="run",
        lease=plans.WorkItemLease(
            run_id="run",
            worker_id=claim["lease_holder_ref"],
            lease_epoch=4,
            fencing_token=938491,
        ),
        claim=claim,
        context_id="a2a-context-" + "a" * 64,
        owner_graph="owner-graph",
    )
    work_engine = SimpleNamespace(graph_name="owner-graph")
    context = SimpleNamespace(
        caller=caller,
        services={
            "task_plan_config": host.task_plan_config,
            "task_plan_work_engine": work_engine,
            "task_plan_model": object(),
        },
        operation_services=host.services,
    )
    plan = {
        **PROPOSAL,
        "max_concurrency": 2,
        "max_steps": 4,
        "plan_digest": "digest",
        "input_digest": "input",
        "policy_digest": "policy",
        "policy_revision": "1",
        "total_tasks": 1,
    }
    parsed = plans.PlanRunParams.model_validate({"plan": PROPOSAL})
    _, eg = task_plan_runtime._contracts()

    async def execute(*_args, **_kwargs):
        owner = agent_runs.current_native_run(
            SimpleNamespace(run_id="run", metadata={"session": session})
        )
        assert owner is not None
        assert owner.work_item_id == "work"
        assert owner.owner_graph == "owner-graph"
        return {
            "status": "completed",
            "error": None,
            "output": "done",
            "started_order": ["answer"],
            "tasks": {"answer": {"status": "completed", "output": "done"}},
            "fleet_calls": [],
        }

    monkeypatch.setattr(plans, "execute_task_plan", execute)
    monkeypatch.setattr(
        plans,
        "import_module",
        lambda name: eg if name == "epistemic_graph.generated.coordination" else None,
    )
    published, run = await plans._publish_and_execute_native_plan(
        context, admission, plan, PROPOSAL, parsed
    )
    assert published == plan
    assert run["status"] == "completed"
    assert len(state["published"]) == 1
    assert state["published"][0]["snapshot"]["run_id"] == "run"
    assert state["published"][0]["snapshot"]["task_plan"] == plan


@pytest.mark.asyncio
async def test_native_plan_publication_caps_expiry_at_claim_lease(chain, monkeypatch):
    host, state, _ = chain
    _, eg = task_plan_runtime._contracts()
    principal = fixtures.caller(
        effective_scopes=frozenset({"kg:write", "query:decide"})
    )
    owner = SimpleNamespace(
        run_id="run",
        claim={**CLAIM, "lease_expires_at_ms": 1_200},
        owner_graph="owner-graph",
    )
    monkeypatch.setattr(task_plan_runtime.time, "time", lambda: 1.0)

    await task_plan_runtime._publish(
        host, principal, owner, {"plan_digest": "digest"}, PROPOSAL, eg
    )

    assert state["published"][0]["snapshot"]["observed_at_ms"] == 1_000
    assert state["published"][0]["snapshot"]["expires_at_ms"] == 1_200


@pytest.mark.asyncio
async def test_native_plan_publication_refuses_expired_claim_before_read(
    chain, monkeypatch
):
    host, state, _ = chain
    _, eg = task_plan_runtime._contracts()
    principal = fixtures.caller(
        effective_scopes=frozenset({"kg:write", "query:decide"})
    )
    owner = SimpleNamespace(
        run_id="run",
        claim={**CLAIM, "lease_expires_at_ms": 1_000},
        owner_graph="owner-graph",
    )
    monkeypatch.setattr(task_plan_runtime.time, "time", lambda: 1.0)

    with pytest.raises(task_plan_runtime.OperationRefused, match="UNAVAILABLE"):
        await task_plan_runtime._publish(
            host, principal, owner, {"plan_digest": "digest"}, PROPOSAL, eg
        )

    assert state["published"] == []


@pytest.mark.asyncio
async def test_exhausted_budget_does_not_call_model(chain):
    host, state, calls = chain
    host = replace(
        host, task_plan_config=replace(host.task_plan_config, request_limit=1)
    )
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        with pytest.raises(Exception, match="BUDGET_EXHAUSTED"):
            await agent_runs.decide_run(RunRequest(task="question", run_id="run"))
    assert not calls and not state["published"]


@pytest.mark.parametrize(
    "extra",
    ["policy", "tenant", "max_steps", "receipt", "worker_id", "owner_graph", "graph"],
)
def test_model_cannot_supply_host_fields(extra):
    with pytest.raises(ValueError):
        task_plan_runtime.Proposal.model_validate({**PROPOSAL, extra: "forged"})


@pytest.mark.asyncio
async def test_failed_publication_starts_no_dependency_task(chain, monkeypatch):
    host, state, calls = chain
    au, eg = task_plan_runtime._contracts()
    eg.send_publish_current_plan = AsyncMock(side_effect=RuntimeError("stale fence"))
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        with pytest.raises(RuntimeError, match="stale fence"):
            await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="question", run_id="run"
                ),
                session=SESSION,
            )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_readback_mismatch_starts_no_dependency_task(chain):
    host, state, calls = chain
    au, eg = task_plan_runtime._contracts()
    read = eg.send_get_current_plan

    async def corrupt(*args):
        response = await read(*args)
        if response is not None:
            return SimpleNamespace(
                model_dump=lambda **k: {**state["snapshot"], "task_plan": {}}
            )
        return response

    eg.send_get_current_plan = corrupt
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        with pytest.raises(RuntimeError, match="INVALID_TASK_PLAN_RECEIPT"):
            await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="question", run_id="run"
                ),
                session=SESSION,
            )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_publication_receipt_for_another_run_is_refused(chain):
    host, state, calls = chain
    _, eg = task_plan_runtime._contracts()
    publish = eg.send_publish_current_plan

    async def wrong_run(*args):
        response = await publish(*args)
        return SimpleNamespace(
            run_id="different-run",
            revision=response.revision,
        )

    eg.send_publish_current_plan = wrong_run
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        with pytest.raises(RuntimeError, match="INVALID_TASK_PLAN_RECEIPT"):
            await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="question", run_id="run"
                ),
                session=SESSION,
            )
    assert len(calls) == 1
    assert state["published"]


@pytest.mark.asyncio
async def test_publication_receipt_for_another_revision_is_refused(chain):
    host, state, calls = chain
    _, eg = task_plan_runtime._contracts()
    publish = eg.send_publish_current_plan

    async def wrong_revision(*args):
        response = await publish(*args)
        return SimpleNamespace(run_id="run", revision=response.revision + 1)

    eg.send_publish_current_plan = wrong_revision
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        with pytest.raises(RuntimeError, match="INVALID_TASK_PLAN_RECEIPT"):
            await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="question", run_id="run"
                ),
                session=SESSION,
            )
    assert len(calls) == 1
    assert state["published"]


@pytest.mark.asyncio
async def test_latest_scaffold_is_not_an_original_publication_retry(chain):
    host, state, calls = chain
    canonical = {"plan_digest": "digest"}
    state["snapshot"] = {
        "run_id": "run",
        "revision": 9,
        "items": [{"text": "advanced", "status": "done"}],
        "task_plan": canonical,
    }
    _, eg = task_plan_runtime._contracts()
    owner = SimpleNamespace(run_id="run", claim=CLAIM, owner_graph="owner-graph")
    principal = fixtures.caller(
        effective_scopes=frozenset({"kg:write", "query:decide"})
    )
    with pytest.raises(
        task_plan_runtime.OperationRefused,
        match="TASK_PLAN_RETRY_REQUIRES_ORIGINAL_PUBLICATION",
    ):
        await task_plan_runtime._publish(
            host, principal, owner, canonical, PROPOSAL, eg
        )
    assert state["published"] == []


@pytest.mark.asyncio
async def test_planner_consumption_preserves_original_bounds(chain):
    host, state, calls = chain
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        plan = await agent_runs.decide_run(RunRequest(task="question", run_id="run"))
    assert plan.budget.request_limit == host.task_plan_config.request_limit
    assert plan.budget.total_tokens_limit == host.task_plan_config.total_tokens_limit
    assert plan.budget.timeout_seconds == host.task_plan_config.timeout_seconds
    assert plan.initial_usage.requests == 1
    assert plan.initial_usage.input_tokens == 7
    assert plan.initial_usage.output_tokens == 3
    assert plan.initial_usage.total_tokens == 10
    assert plan.initial_elapsed_seconds > 0
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("graph", [None, "", " other "])
async def test_missing_or_malformed_private_graph_refuses_before_read(chain, graph):
    host, state, _ = chain
    _, eg = task_plan_runtime._contracts()
    eg.send_get_current_plan = AsyncMock()
    principal = fixtures.caller(
        effective_scopes=frozenset({"kg:write", "query:decide"})
    )
    with pytest.raises(task_plan_runtime.OperationRefused, match="UNAVAILABLE"):
        await task_plan_runtime._publish(
            host,
            principal,
            SimpleNamespace(run_id="run", claim=CLAIM, owner_graph=graph),
            {"plan_digest": "digest"},
            PROPOSAL,
            eg,
        )
    eg.send_get_current_plan.assert_not_awaited()
    assert not state["published"]


@pytest.mark.asyncio
async def test_private_graph_does_not_override_engine_capability_refusal(chain):
    host, state, _ = chain
    _, eg = task_plan_runtime._contracts()
    eg.send_get_current_plan = AsyncMock(
        side_effect=PermissionError("graph capability denied")
    )
    principal = fixtures.caller(
        effective_scopes=frozenset({"kg:write", "query:decide"})
    )
    with pytest.raises(PermissionError, match="graph capability denied"):
        await task_plan_runtime._publish(
            host,
            principal,
            SimpleNamespace(run_id="run", claim=CLAIM, owner_graph="foreign"),
            {"plan_digest": "digest"},
            PROPOSAL,
            eg,
        )
    assert eg.send_get_current_plan.await_args.args[2] == "foreign"
    assert not state["published"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["publication", "readback"])
async def test_cancellation_revokes_scope_without_dispatching_children(chain, phase):
    import asyncio
    from contextvars import copy_context

    host, state, calls = chain
    _, eg = task_plan_runtime._contracts()
    reached = asyncio.Event()
    contexts = []
    publish, read = eg.send_publish_current_plan, eg.send_get_current_plan

    async def paused_publish(*args):
        receipt = await publish(*args)
        if phase == "publication":
            reached.set()
            await asyncio.Future()
        return receipt

    async def paused_read(*args):
        snapshot = await read(*args)
        if phase == "readback" and snapshot is not None:
            reached.set()
            await asyncio.Future()
        return snapshot

    eg.send_publish_current_plan = paused_publish
    eg.send_get_current_plan = paused_read

    async def execute():
        with agent_runs.native_run_scope(
            run_id="run",
            task_id="task",
            context_id="context",
            work_item_id="work",
            session=SESSION,
            claim=CLAIM,
            owner_graph="owner-graph",
            hosted_invocation=host,
        ):
            contexts.append(copy_context())
            return await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="question", run_id="run"
                ),
                session=SESSION,
            )

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(calls) == 1
    assert len(state["published"]) == 1
    assert state["snapshot"]["task_plan"]["plan_digest"] == "digest"
    with pytest.raises(RuntimeError, match="native run scope is closed"):
        contexts[0].run(
            agent_runs.current_native_run, RunRequest(task="question", run_id="run")
        )


@pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")
@pytest.mark.parametrize("setting", ["on", "off"])
async def test_trusted_scaffold_option_reaches_actual_au_callback(
    chain, setting, monkeypatch
):
    from contextlib import asynccontextmanager

    host, state, calls = chain
    host = replace(
        host, task_plan_config=replace(host.task_plan_config, plan_scaffold=setting)
    )
    published = []

    def scaffold_model(messages, info):
        from pydantic_ai.messages import ToolCallPart

        calls.append((messages, info))
        if len(calls) == 1:
            assert not info.function_tools
            return ModelResponse(parts=[TextPart(json.dumps(PROPOSAL))])
        assert [tool.name for tool in info.function_tools] == (
            ["update_plan"] if setting == "on" else []
        )
        if setting == "on" and len(calls) == 2:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "update_plan",
                        {"items": [{"text": "inspect", "status": "done"}]},
                        tool_call_id="plan-update",
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    model = FunctionModel(scaffold_model)
    monkeypatch.setattr(agent_runs, "resolve_model", lambda name: model)
    host_ports.register_model_provider(lambda name: model)

    class Publisher:
        async def publish(self, revision, items):
            published.append((revision, items))

    @asynccontextmanager
    async def factory(request):
        assert request.metadata["session"] is SESSION
        assert state["published"][0]["snapshot"]["task_plan"]["plan_digest"] == "digest"
        yield Publisher()

    host_ports.register_optional_scaffold_publication_factory(factory)
    try:
        with agent_runs.native_run_scope(
            run_id="run",
            task_id="task",
            context_id="context",
            work_item_id="work",
            session=SESSION,
            claim=CLAIM,
            owner_graph="owner-graph",
            hosted_invocation=host,
        ):
            result = await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="question", run_id="run"
                ),
                session=SESSION,
            )
        assert result.output == "done"
        assert len(calls) == (3 if setting == "on" else 2)
        assert [revision for revision, _ in published] == (
            [0, 1] if setting == "on" else []
        )
        if setting == "on":
            assert published[0][1] == ()
            assert published[1][1][0].text == "inspect"
    finally:
        host_ports.register_optional_scaffold_publication_factory(None)


@pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")
@pytest.mark.parametrize("setting, untrusted", [("off", "on"), ("on", "off")])
async def test_only_operator_scaffold_field_changes_base_harness(
    chain, setting, untrusted
):
    from meristem.runtime import default_run_plan

    host, _, _ = chain
    host = replace(
        host, task_plan_config=replace(host.task_plan_config, plan_scaffold=setting)
    )
    request = RunRequest(
        task="question", run_id="run", metadata={"plan_scaffold": untrusted}
    )
    base = default_run_plan(request, {"agent_model": "test"})
    base = replace(
        base,
        harness=replace(
            base.harness,
            retries=2,
            response_format="json",
            model_settings={"temperature": 0.123},
        ),
    )
    with agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    ):
        owner = agent_runs.current_native_run(request)
        plan = await task_plan_runtime.admitted_run_plan(request, base, owner)
    assert plan.harness == replace(base.harness, plan_scaffold=setting)
