"""Actual AU execution through bound Arbora host factories; injected native transport."""

import contextlib
from types import SimpleNamespace

import pytest
from agent_utilities import host_ports
from agent_utilities.graph import AgentRunExecutor, RunPlan
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from arbora.admitted_run import bind_admitted_claim
from arbora.agent_runs import AgentRunPort, bind_run_ports
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest


@pytest.mark.asyncio
async def test_real_executor_preserves_bound_session_and_completed_history(monkeypatch):
    import arbora.conversation_history as history
    import arbora.run_checkpoints as checkpoints

    monkeypatch.setattr(history, "use_session", lambda _: contextlib.nullcontext())
    monkeypatch.setattr(checkpoints, "use_session", lambda _: contextlib.nullcontext())
    snapshots = {}
    rows = {}
    seen = []
    reads = []
    session = SimpleNamespace(tenant="tenant", engine_verified_context=lambda: None)

    async def get_checkpoint(**params):
        reads.append(params)
        return snapshots.get(params["work_item_id"])

    async def publish_checkpoint(**params):
        item = params["work_item_id"]
        revision = (params["expected_revision"] or 0) + 1
        snapshots[item] = dict(
            params["snapshot"], revision=revision, work_item=rows[item]
        )
        return dict(
            revision=revision,
            replayed=False,
            work_item_id=item,
            run_id=params["run_id"],
            payload_digest=params["snapshot"]["payload_digest"],
        )

    async def listing(**params):
        assert params["tenant"] == "tenant"
        assert params["metadata_match"]["a2a_owner_ref"] == "owner"
        return {"items": list(rows.values()), "next_cursor": None}

    client = SimpleNamespace(
        get_checkpoint=get_checkpoint,
        publish_checkpoint=publish_checkpoint,
        list=listing,
    )
    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda _: SimpleNamespace(
                async_client=SimpleNamespace(work_items=client)
            )
        )
    )

    def respond(messages, info):
        seen.append(messages)
        return ModelResponse(parts=[TextPart("remember blue")])

    model = FunctionModel(respond)

    def decide(request):
        assert request.metadata["session"] is session
        return RunPlan(model=model, steps=(request.task,))

    for name in (
        "_DECIDE",
        "_MODELS",
        "_EVENTS",
        "_SCAFFOLD_PUBLICATION",
        "_CHECKPOINTS",
        "_RUN_HISTORY",
    ):
        monkeypatch.setattr(host_ports, name, getattr(host_ports, name))
    bind_run_ports()
    host_ports.register_run_decision_provider(decide)
    host_ports.register_run_event_sink(None)
    port = AgentRunPort(AgentRunExecutor())

    async def turn(run_id, required=False):
        item = "item-" + run_id
        rows.setdefault(
            item,
            {
                "work_item_id": item,
                "kind": "orchestrator_task",
                "status": "succeeded",
                "updated_at_ms": len(rows) + 1,
                "metadata": {
                    "a2a_schema": "arbora-a2a-unary-v1",
                    "a2a_owner_ref": "owner",
                    "a2a_context_id": "context",
                    "a2a_run_id": run_id,
                },
            },
        )
        claim = dict(
            claimed=True,
            tenant="tenant",
            work_item_id=item,
            lease_holder_ref="holder",
            lease_epoch=1,
            fencing_token=1,
            lease_expires_at_ms=2**63,
        )
        with bind_admitted_claim(
            engine,
            session=session,
            run_id=run_id,
            work_item_id=item,
            claim=claim,
            context_id="context",
            owner_ref="owner",
            history_required=required,
        ):
            return await port.execute_agent(
                AgentExecutionRequest(agent_name="agent", task=run_id, run_id=run_id),
                session=session,
            )

    first = await turn("first")
    second = await turn("second", True)
    assert first.output == second.output == "remember blue"
    assert len(seen) == 2
    assert any(
        getattr(part, "content", "") == "remember blue"
        for message in seen[1]
        for part in message.parts
    )
    prior_read = next(
        read
        for read in reads
        if read["work_item_id"] == "item-first" and "current_claim" not in read
    )
    assert prior_read["run_id"] == "first"
    replay = await turn("second", True)
    assert replay.output == second.output and len(seen) == 2
