"""Native admission/claim source wiring; the engine below is a transport double."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from agent_utilities import host_ports
from agent_utilities.harness.plan import PlanState

from arbora import work_items
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
from arbora.api.contracts.session import use_session
from tests.a2a.test_operation_bridge import session
from tests.test_agents_run_end_to_end import FakeWorkItems
from tests.test_current_plan_publication import register_publication_fixture

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")
NATIVE_ID = "work-item:" + "a" * 64


class NativeItems(FakeWorkItems):
    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        assert request["work_item_id"] is None
        assert request["run_id"].startswith("a2a-")
        self.run_id = request["run_id"]
        return super().submit({**request, "work_item_id": NATIVE_ID})

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        result = super().claim(request)
        return {
            **result,
            "work_item_id": NATIVE_ID,
            "lease_expires_at_ms": int(time.time() * 1000) + 60_000,
        }

    def list(self, **request: Any) -> dict[str, Any]:
        assert request["metadata_match"]["a2a_owner_ref"]
        return {"items": list(self.rows.values()), "next_cursor": None}

    def cancel(self, **request: Any) -> dict[str, Any]:
        self.rows[request["work_item_id"]]["status"] = "cancelled"
        return {"status": "cancelled"}


@pytest.mark.asyncio
async def test_admission_claim_au_callback_and_native_task_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from epistemic_graph.generated import coordination

    monkeypatch.setattr(work_items, "supports_run_admission", lambda: True)
    calls = []

    async def send(
        client: Any, params: Any, graph: str, *, idempotency_key: str
    ) -> Any:
        assert idempotency_key == params["request"]["idempotency_key"]
        calls.append(params["request"])
        return params

    monkeypatch.setattr(coordination, "send_publish_current_plan", send, raising=False)
    monkeypatch.setattr(
        coordination, "decode_publish_current_plan", lambda x: x, raising=False
    )
    register_publication_fixture()
    items = NativeItems()

    async def submit(
        client: Any, params: Any, graph: str, *, idempotency_key: str
    ) -> Any:
        assert idempotency_key == params["request"]["idempotency_key"]
        assert graph == "__control__"
        return items.submit(params["request"])

    monkeypatch.setattr(coordination, "send_submit_work_item", submit)
    monkeypatch.setattr(
        coordination,
        "decode_submit_work_item",
        lambda result: SimpleNamespace(model_dump=lambda **kw: result),
    )
    engine = SimpleNamespace(
        work_items=items,
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(async_client=object())
        ),
    )

    class Executor:
        async def execute_agent(self, request: Any, *, session: Any) -> Any:
            assert request.run_id != NATIVE_ID
            au_request = SimpleNamespace(
                run_id=request.run_id, metadata={"session": session}
            )
            async with host_ports.scaffold_publication_scope(au_request, PlanState()):
                pass
            return SimpleNamespace(run_id=request.run_id, output="done")

    authority = WorkItemA2AAuthority(lambda: engine, executor=Executor())
    try:
        with use_session(session(frozenset({"kg:write", "kg:read"}))):
            task = await authority.dispatch(
                message=A2AMessage(
                    role="user", message_id="m", parts=[A2ATextPart(text="inspect")]
                ),
                idempotency_key="key",
                decision=A2ARouteDecision(
                    agent_name="helper", selection_mode="explicit"
                ),
            )
            assert task.id == NATIVE_ID
            assert task.status.state == "completed"
            assert task.metadata["arbora"]["runId"] == items.run_id
            assert calls[0]["snapshot"]["run_id"] == items.run_id
            assert calls[0]["fencing_token"] == 1
            observed = await authority.get(NATIVE_ID)
            assert observed is not None and observed.id == NATIVE_ID
            page, cursor = await authority.list(cursor=None, limit=10)
            assert [t.id for t in page] == [NATIVE_ID] and cursor is None
            items.rows[NATIVE_ID]["status"] = "ready"
            assert (await authority.cancel(NATIVE_ID)).status.state == "canceled"
            items.rows[NATIVE_ID]["metadata"]["a2a_owner_ref"] = "foreign"
            assert await authority.get(NATIVE_ID) is None
    finally:
        host_ports.register_scaffold_publication_factory(None)


@pytest.mark.parametrize(
    "task_id", ["work-item:bad", "work-item:" + "g" * 64, "a2a-bad"]
)
def test_malformed_native_or_legacy_ids_do_not_fall_back(task_id: str) -> None:
    with pytest.raises(ValueError):
        WorkItemA2AAuthority._work_item_id(task_id)


@pytest.mark.parametrize("conflict", [False, True])
def test_upgraded_admission_reuses_legacy_task_without_native_submit(
    monkeypatch, conflict
):
    from arbora.a2a.authority import A2AIdempotencyConflict

    monkeypatch.setattr(work_items, "supports_run_admission", lambda: True)
    caller = session(frozenset({"kg:read", "kg:write"}))
    owner = WorkItemA2AAuthority._owner_ref(caller)
    run_id = "a2a-" + "b" * 64
    item_id = WorkItemA2AAuthority._work_item_id(run_id)
    items = FakeWorkItems()
    metadata = {
        "a2a_schema": "arbora.a2a.task.v1",
        "a2a_owner_ref": owner,
        "a2a_request_digest": "original",
    }
    # Use the production metadata schema rather than a parallel authority model.
    from arbora.a2a.authority import _A2A_METADATA_SCHEMA

    metadata["a2a_schema"] = _A2A_METADATA_SCHEMA
    items.rows[item_id] = {
        "work_item_id": item_id,
        "kind": "orchestrator_task",
        "metadata": metadata,
    }
    with use_session(caller):

        def retry():
            return WorkItemA2AAuthority._submit(
                SimpleNamespace(work_items=items),
                task_id=run_id,
                session=caller,
                owner_ref=owner,
                description="retry",
                metadata={
                    **metadata,
                    "a2a_request_digest": "changed" if conflict else "original",
                },
            )

        if conflict:
            with pytest.raises(A2AIdempotencyConflict):
                retry()
        else:
            assert retry() == (item_id, False)
    assert list(items.rows) == [item_id]
