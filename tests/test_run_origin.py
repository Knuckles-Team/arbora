"""Live browser proposal provenance follows host calls, not caller JSON."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from arbora import run_origin, work_items
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.agent_runs import AgentRunPort
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
from arbora.api.contracts.session import GraphSession, use_session
from arbora.mcp_server.runtime import _call_native_intent
from arbora.security.actor_context import ActorContext, ActorType

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015")


def session(owner: str = "owner", tenant: str = "test") -> GraphSession:
    return GraphSession(
        actor=ActorContext(
            actor_id=owner,
            actor_type=ActorType.HUMAN,
            tenant_id=tenant,
            authenticated=True,
            roles=("kg:read", "kg:write"),
        ),
        tenant=tenant,
        scopes=frozenset({"kg:read", "kg:write"}),
    )


@pytest.mark.asyncio
async def test_copied_context_does_not_extend_call_lifetime() -> None:
    admitted = session()
    release = asyncio.Event()

    async def copied() -> None:
        await release.wait()
        with pytest.raises(PermissionError):
            run_origin.require_native_call_origin()

    with (
        use_session(admitted),
        run_origin.agent_run_origin_scope(admitted, "web", "run"),
    ):
        with run_origin.native_call_origin_scope("run"):
            origin = run_origin.require_native_call_origin()
            task = asyncio.create_task(copied())
        assert not origin.is_live()
        release.set()
        await task
    assert not run_origin._runs


@pytest.mark.asyncio
async def test_origin_refuses_missing_foreign_and_narrowed_authority() -> None:
    admitted = session()
    with use_session(admitted):
        with run_origin.native_call_origin_scope("run"):
            with pytest.raises(PermissionError):
                run_origin.require_native_call_origin()
        with run_origin.agent_run_origin_scope(admitted, "web", "run"):
            with run_origin.native_call_origin_scope("different"):
                with pytest.raises(PermissionError):
                    run_origin.require_native_call_origin()
            with run_origin.native_call_origin_scope("run"):
                for other in (
                    session("foreign"),
                    replace(admitted, scopes=frozenset({"kg:read"})),
                ):
                    with use_session(other), pytest.raises(PermissionError):
                        run_origin.require_native_call_origin()


@pytest.mark.asyncio
async def test_revoke_is_owner_scoped_immediate_and_cross_thread_safe() -> None:
    admitted = session()
    closed = asyncio.Event()
    with (
        use_session(admitted),
        run_origin.agent_run_origin_scope(admitted, "web", "run"),
    ):
        with run_origin.native_call_origin_scope("run"):
            origin = run_origin.require_native_call_origin()
            origin.subscribe_close(closed.set)
            for other in (session("foreign"), session(tenant="other")):
                with use_session(other):
                    run_origin.revoke_run_origins(other, "run")
                assert origin.is_live()
            await asyncio.to_thread(run_origin.revoke_run_origins, admitted, "run")
            assert not origin.is_live()
            await asyncio.wait_for(closed.wait(), 1)


@pytest.mark.asyncio
async def test_native_call_cancel_closes_origin_and_unsubscribes() -> None:
    admitted = session()
    ready = asyncio.Event()
    captured = []

    async def verb(**kwargs: object) -> None:
        captured.append(run_origin.require_native_call_origin())
        ready.set()
        await asyncio.Event().wait()

    with (
        use_session(admitted),
        run_origin.agent_run_origin_scope(admitted, "web", "run"),
    ):
        parent = run_origin._run.get()
        task = asyncio.create_task(_call_native_intent("act", verb, "run", {}))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not captured[0].is_live()
        assert parent is not None and not parent._callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("status", ["running", "cancelled"])
async def test_a2a_durable_cancel_revokes_real_pending_native_call(
    monkeypatch: pytest.MonkeyPatch, native: bool, status: str
) -> None:
    admitted = session()
    run_id = "a2a-" + "a" * 64
    task_id = "work-item:" + "b" * 64 if native else run_id
    ready = asyncio.Event()
    captured = []
    durable = {"status": status}
    authority = WorkItemA2AAuthority(lambda: object())
    item: dict[str, Any] = {
        "kind": "orchestrator_task",
        "metadata": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": authority._owner_ref(admitted),
        },
    }
    if native:
        item["metadata"]["a2a_run_id"] = run_id
    monkeypatch.setattr(work_items, "get_work_item", lambda *_: {**item, **durable})
    monkeypatch.setattr(authority, "_project", lambda value, **_: value)

    def cancel(*args: object) -> bool:
        durable["status"] = "cancelled"
        return True

    monkeypatch.setattr(work_items, "cancel_work_item", cancel)

    async def verb(**kwargs: object) -> None:
        origin = run_origin.require_native_call_origin()
        captured.append(origin)
        current = asyncio.current_task()
        assert current is not None

        def cancel_current() -> None:
            current.cancel()

        unsubscribe = origin.subscribe_close(cancel_current)
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            unsubscribe()

    class Executor:
        async def execute_agent(
            self, request: AgentExecutionRequest, **kwargs: object
        ) -> None:
            await _call_native_intent("act", verb, request.run_id or "", {})

    with use_session(admitted):
        pending = asyncio.create_task(
            AgentRunPort(Executor()).execute_agent(
                AgentExecutionRequest(agent_name="web", task="navigate", run_id=run_id),
                session=admitted,
            )
        )
        await asyncio.wait_for(ready.wait(), 1)
        assert captured[0].agent_name == "web"
        await asyncio.wait_for(authority.cancel(task_id), 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 2)
    assert not captured[0].is_live()
    assert not run_origin._runs


@pytest.mark.asyncio
async def test_run_without_explicit_identity_does_not_fabricate_origin() -> None:
    admitted = session()

    class Executor:
        async def execute_agent(
            self, request: AgentExecutionRequest, **kwargs: object
        ) -> SimpleNamespace:
            with run_origin.native_call_origin_scope("generated"):
                with pytest.raises(PermissionError):
                    run_origin.require_native_call_origin()
            return SimpleNamespace(
                run_id="generated", output="ok", execution_mode="direct", usage=None
            )

    with use_session(admitted):
        await AgentRunPort(Executor()).execute_agent(
            AgentExecutionRequest(agent_name="web", task="navigate"),
            session=admitted,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign,committed", [(True, True), (False, False)])
async def test_refused_a2a_cancel_does_not_revoke_origin(
    monkeypatch: pytest.MonkeyPatch, foreign: bool, committed: bool
) -> None:
    from arbora.a2a.authority import A2ATaskNotCancelable

    admitted = session()
    task_id = "a2a-" + "b" * 64
    authority = WorkItemA2AAuthority(lambda: object())
    item: dict[str, Any] = {
        "kind": "orchestrator_task",
        "status": "running",
        "metadata": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": authority._owner_ref(
                session("foreign") if foreign else admitted
            ),
        },
    }
    monkeypatch.setattr(work_items, "get_work_item", lambda *_: item)
    monkeypatch.setattr(work_items, "cancel_work_item", lambda *_: committed)
    with (
        use_session(admitted),
        run_origin.agent_run_origin_scope(admitted, "web", task_id),
    ):
        with run_origin.native_call_origin_scope(task_id):
            origin = run_origin.require_native_call_origin()
            with pytest.raises(A2ATaskNotCancelable):
                await asyncio.wait_for(authority.cancel(task_id), 2)
            assert origin.is_live()


@pytest.mark.asyncio
async def test_unsubscribe_and_already_closed_subscription() -> None:
    origin = run_origin.NativeCallOrigin(session(), "web", "run")
    removed = asyncio.Event()
    delivered = asyncio.Event()
    unsubscribe = origin.subscribe_close(removed.set)
    unsubscribe()
    origin.close()
    origin.subscribe_close(delivered.set)
    await asyncio.wait_for(delivered.wait(), 1)
    assert not removed.is_set()
