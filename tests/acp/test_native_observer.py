"""Trusted observation never creates native execution authority."""

import asyncio
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from arbora import agent_runs
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
from arbora.api.contracts.session import ActorContext, GraphSession, use_session

pytestmark = pytest.mark.spec("ARBORA-ACP-R001.9.1")


def identity(name):
    return GraphSession(
        actor=ActorContext(actor_id=name, tenant_id=name, authenticated=True),
        tenant=name,
        scopes=frozenset({"kg:write"}),
    )


@contextmanager
def admitted(session, run_id):
    with agent_runs.native_run_scope(
        run_id=run_id,
        task_id=f"task:{run_id}",
        context_id=f"context:{run_id}",
        work_item_id=f"work:{run_id}",
        session=session,
        claim={
            "claimed": True,
            "tenant": session.tenant,
            "lease_holder_ref": "test-worker",
            "lease_epoch": 1,
            "fencing_token": 1,
        },
    ):
        yield


class Executor:
    def __init__(self):
        self.calls = []
        self.observers = []

    async def execute_agent(self, request, *, session, stream_observer=None):
        self.calls.append((request, session))
        if stream_observer:
            self.observers.append(stream_observer)
            await asyncio.sleep(0)
            await stream_observer({"private": request.run_id})
        return SimpleNamespace(
            run_id=request.run_id, output="answer", execution_mode="graph", usage=None
        )


async def test_concurrent_observers_keep_exact_native_request_and_identity():
    executor = Executor()
    port = agent_runs.AgentRunPort(executor)
    seen = {"alice": [], "bob": []}

    async def run(name):
        session = identity(name)
        request = AgentExecutionRequest(
            agent_name="helper", task="prompt", run_id=name, allowed_tools=("find",)
        )

        async def observer(event):
            seen[name].append(event)

        with use_session(session), agent_runs.native_stream_scope(session, observer):
            with admitted(session, name):
                result = await port.execute_agent(request, session=session)
                assert result.output == "answer"
                assert executor.calls[-1][0].allowed_tools == ("find",)
        return request, session

    originals = await asyncio.gather(run("alice"), run("bob"))
    assert seen == {"alice": [{"private": "alice"}], "bob": [{"private": "bob"}]}
    assert executor.calls == originals
    for callback in executor.observers:
        with pytest.raises(RuntimeError, match="closed"):
            await callback({"private": "after close"})


@pytest.mark.parametrize("case", ["unadmitted", "foreign", "reused"])
async def test_observer_cannot_supply_or_reuse_admission(case):
    executor = Executor()
    port = agent_runs.AgentRunPort(executor)
    session = identity("alice")
    request = AgentExecutionRequest(agent_name="helper", task="prompt", run_id="run")

    async def observe(event):
        pass

    with use_session(session), agent_runs.native_stream_scope(session, observe):
        if case == "unadmitted":
            with pytest.raises(RuntimeError, match="matching admitted"):
                await port.execute_agent(request, session=session)
        else:
            with admitted(session, "run"):
                if case == "reused":
                    await port.execute_agent(request, session=session)
                with pytest.raises(RuntimeError):
                    await port.execute_agent(
                        request,
                        session=identity("bob") if case == "foreign" else session,
                    )
    assert len(executor.calls) == (1 if case == "reused" else 0)


async def test_observer_error_is_not_swallowed_or_fabricated_as_success():
    session = identity("alice")
    executor = Executor()

    async def observe(event):
        raise ValueError("closed receiver")

    with use_session(session), agent_runs.native_stream_scope(session, observe):
        with admitted(session, "run"):
            with pytest.raises(ValueError, match="closed receiver"):
                await agent_runs.AgentRunPort(executor).execute_agent(
                    AgentExecutionRequest(
                        agent_name="helper", task="prompt", run_id="run"
                    ),
                    session=session,
                )


async def test_cancellation_drains_observer_without_success():
    session = identity("alice")
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def observe(event):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    with use_session(session), agent_runs.native_stream_scope(session, observe):
        with admitted(session, "run"):
            task = asyncio.create_task(
                agent_runs.AgentRunPort(Executor()).execute_agent(
                    AgentExecutionRequest(
                        agent_name="helper", task="prompt", run_id="run"
                    ),
                    session=session,
                )
            )
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert drained.is_set()


async def test_unupgraded_executor_refuses_before_execution():
    session = identity("alice")
    calls = []

    class OldExecutor:
        async def execute_agent(self, request, *, session):
            calls.append(request)

    async def observe(event):
        pass

    with use_session(session), agent_runs.native_stream_scope(session, observe):
        with admitted(session, "run"):
            with pytest.raises(RuntimeError, match="UPGRADE_REQUIRED"):
                await agent_runs.AgentRunPort(OldExecutor()).execute_agent(
                    AgentExecutionRequest(
                        agent_name="helper", task="prompt", run_id="run"
                    ),
                    session=session,
                )
    assert not calls


async def test_separately_admitted_child_run_does_not_inherit_parent_observer():
    session = identity("alice")
    seen = []
    child_observers = []

    async def observe(event):
        seen.append(event)

    class NestedExecutor(Executor):
        async def execute_agent(self, request, *, session, stream_observer=None):
            if request.run_id == "parent":

                async def child():
                    with admitted(session, "child"):
                        return await port.execute_agent(
                            AgentExecutionRequest(
                                agent_name="helper", task="child", run_id="child"
                            ),
                            session=session,
                        )

                assert (await asyncio.create_task(child())).output == "answer"
            else:
                child_observers.append(stream_observer)
            return await super().execute_agent(
                request, session=session, stream_observer=stream_observer
            )

    port = agent_runs.AgentRunPort(NestedExecutor())
    with use_session(session), agent_runs.native_stream_scope(session, observe):
        with admitted(session, "parent"):
            await port.execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="parent", run_id="parent"
                ),
                session=session,
            )
    assert child_observers == [None]
    assert seen == [{"private": "parent"}]


@pytest.mark.parametrize(
    "callback_fails",
    [
        False,
        "unavailable",
        "error",
        "cancel",
        "readback",
        "missing_readback",
        "completed",
    ],
)
async def test_admission_callback_receives_owner_id_before_execution(
    monkeypatch, callback_fails
):
    from unittest.mock import AsyncMock

    from arbora import work_items
    from arbora.a2a.authority import WorkItemA2AAuthority
    from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
    from arbora.mcp_server.task_plan import TaskPlanConfig

    session = identity("alice")
    item_id = "work-item:" + "e" * 64
    context_id = "a2a-context-" + "c" * 64
    rows, receipts, started = {}, [], []
    authority = WorkItemA2AAuthority(
        lambda: object(), task_plan_config=TaskPlanConfig({}, {}, 1, 2)
    )

    def submit(engine, **kwargs):
        rows[item_id] = {
            "work_item_id": item_id,
            "kind": "orchestrator_task",
            "status": "succeeded" if callback_fails == "completed" else "ready",
            "metadata": kwargs["metadata"],
        }
        return item_id, callback_fails != "completed"

    async def run(engine, **kwargs):
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.task_id == kwargs["task_id"] == item_id
        assert receipt.work_item_id == kwargs["item_id"] == item_id
        assert receipt.run_id == kwargs["run_id"] != item_id
        assert receipt.context_id == kwargs["context_id"] == context_id
        started.append(kwargs)

    monkeypatch.setattr(work_items, "submit_work_item_atomic", submit)
    monkeypatch.setattr(authority, "_resolve_context_id", lambda *args: context_id)
    monkeypatch.setattr(authority, "_run", run)
    monkeypatch.setattr(
        authority, "_project_dispatch_task", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(work_items, "get_work_item", lambda engine, key: rows.get(key))

    if callback_fails in {"error", "cancel", "readback", "missing_readback"}:

        def cancel(engine, key):
            rows[key]["status"] = "cancelled"
            return True

        monkeypatch.setattr(work_items, "cancel_work_item", cancel)
    if callback_fails in {"readback", "missing_readback"}:
        original = authority._authorized_item
        reads = []

        def readback(*args):
            value = original(*args)
            reads.append(value)
            if len(reads) != 2:
                return value
            return (
                None
                if callback_fails == "missing_readback"
                else {**value, "status": "ready"}
            )

        monkeypatch.setattr(authority, "_authorized_item", readback)

    async def observe(event):
        pytest.fail("No model execution in this owner-receipt test")

    def admitted_receipt(receipt):
        receipts.append(receipt)
        if callback_fails == "cancel":
            raise asyncio.CancelledError()
        if callback_fails:
            raise RuntimeError("private callback detail")

    if callback_fails == "cancel":
        outcome = pytest.raises(asyncio.CancelledError)
    elif callback_fails:
        message = (
            "admitted task canceled"
            if callback_fails == "error"
            else "settlement unavailable"
        )
        outcome = pytest.raises(RuntimeError, match=message)
    else:
        outcome = nullcontext()
    with (
        outcome,
        use_session(session),
        agent_runs.native_stream_scope(session, observe, on_admitted=admitted_receipt),
    ):
        await authority.dispatch(
            message=A2AMessage(
                role="user",
                message_id="message",
                parts=[A2ATextPart(text="prompt")],
                metadata={
                    "run_id": "forged",
                    "task_id": "forged",
                    "host_transport_protocol": "forged",
                },
            ),
            idempotency_key="test-key",
            decision=A2ARouteDecision(agent_name="helper", selection_mode="explicit"),
        )
    assert len(started) == (0 if callback_fails else 1)
    expected = (
        "cancelled"
        if callback_fails in {"error", "cancel", "readback", "missing_readback"}
        else "ready"
    )
    if callback_fails == "completed":
        expected = "succeeded"
    assert rows[item_id]["status"] == expected
    assert len(receipts) == 1  # No automatic replay.
    assert "host_transport_protocol" not in rows[item_id]["metadata"]
