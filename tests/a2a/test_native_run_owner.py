"""Native owner admission uses engine identities without a durable alias map."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbora import work_items
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
from arbora.mcp_server.task_plan import TaskPlanConfig
from tests.a2a.test_context_continuation import SESSION

pytestmark = pytest.mark.spec("ARBORA-OPS-R001")


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [True, False])
async def test_submit_retry_get_list_cancel_use_returned_engine_id(
    monkeypatch, blocking
):
    rows = {}
    requests = []
    item_id = "work-item:" + "e" * 64

    def submit(engine, **request):
        requests.append(request)
        created = not rows
        rows[item_id] = rows.get(item_id) or {
            "work_item_id": item_id,
            "kind": "orchestrator_task",
            "status": "ready",
            "metadata": request["metadata"],
        }
        return item_id, created

    def cancel(engine, key):
        rows[key]["status"] = "cancelled"
        return True

    engine = SimpleNamespace(
        work_item_graph="private-owner",
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(
                async_client=SimpleNamespace(
                    work_items=SimpleNamespace(get_run=AsyncMock(return_value=None))
                )
            )
        ),
        work_items=SimpleNamespace(
            list=lambda **kw: {"items": list(rows.values()), "next_cursor": None}
        ),
    )
    monkeypatch.setattr(work_items, "submit_work_item_atomic", submit)
    monkeypatch.setattr(work_items, "get_work_item", lambda engine, key: rows.get(key))
    monkeypatch.setattr(work_items, "cancel_work_item", cancel)
    authority = WorkItemA2AAuthority(
        lambda: engine, task_plan_config=TaskPlanConfig({}, {}, 1, 2)
    )
    monkeypatch.setattr(authority, "_session", lambda scope: SESSION)
    monkeypatch.setattr(authority, "_owner_ref", lambda session: "owner")
    run = AsyncMock(return_value=None)
    monkeypatch.setattr(authority, "_run", run)
    message = A2AMessage(
        role="user",
        message_id="one",
        parts=[A2ATextPart(text="hello")],
        metadata={"run_id": "forged", "native_claim": "forged"},
    )
    kwargs = dict(
        message=message,
        blocking=blocking,
        idempotency_key="one",
        decision=A2ARouteDecision(agent_name="helper", selection_mode="explicit"),
    )
    first = await authority.dispatch(**kwargs)
    retry = await authority.dispatch(**kwargs)
    if not blocking:
        await asyncio.gather(*authority._detached)
    assert first.id == retry.id == item_id
    assert requests[0]["work_item_id"] is None
    run_id = requests[0]["run_id"]
    assert run_id == first.metadata["arbora"]["runId"] != first.id
    assert run.await_count == 2
    for call in run.await_args_list:
        assert call.kwargs["task_id"] == item_id
        assert call.kwargs["run_id"] == run_id
        assert call.kwargs["context_id"] == first.context_id
    assert "native_claim" not in requests[0]["metadata"]
    assert (await authority.get(item_id)).id == item_id
    assert (await authority.list(cursor=None, limit=10))[0][0].id == item_id
    assert (await authority.cancel(item_id)).status.state == "canceled"
    durable_get = AsyncMock(wraps=authority.get)
    monkeypatch.setattr(authority, "get", durable_get)
    run.reset_mock()
    assert (await authority.dispatch(**kwargs)).status.state == "canceled"
    durable_get.assert_awaited_once_with(item_id)
    run.assert_not_awaited()


def test_old_client_refuses_native_owner_before_submit(monkeypatch):
    from epistemic_graph.generated.models import SubmitWorkItemRequest

    from arbora.api.invoke.pipeline import OperationRefused

    monkeypatch.setattr(SubmitWorkItemRequest, "model_fields", {})
    with pytest.raises(OperationRefused) as error:
        work_items.submit_work_item_atomic(
            object(), kind="task", idempotency_key="one", run_id="run"
        )
    assert error.value.code == "UPGRADE_REQUIRED"


def test_upgrade_refusal_has_explicit_a2a_error():
    import json

    from arbora.a2a.application import _application_error
    from arbora.api.invoke.pipeline import OperationRefused

    response = _application_error("one", OperationRefused("UPGRADE_REQUIRED"))
    assert response.status_code == 501
    assert "UPGRADE_REQUIRED" in json.loads(bytes(response.body))["error"]["message"]


def test_owner_graph_comes_from_work_item_engine_not_session():
    from arbora.mcp_server.served_engine import ServedEngine

    engine = ServedEngine(object())
    assert (
        work_items.native_owner_graph(engine) == engine.work_item_graph == "__control__"
    )
    assert (
        work_items.native_owner_graph(SimpleNamespace(graph_name="admitted"))
        == "admitted"
    )
    with pytest.raises(work_items.NativeWorkItemRequired):
        work_items.native_owner_graph(SimpleNamespace(graph_name=" bad "))
    with pytest.raises(
        work_items.NativeWorkItemRequired, match="context graph mismatch"
    ):
        work_items._typed_request_context(
            {"tenant_id": "tenant", "graph": "foreign"}, SESSION, "key", "admitted"
        )


@pytest.mark.parametrize("payload_run", ["native-run", "public-work-item"])
def test_durable_reply_uses_native_run_not_public_work_item(payload_run):
    import json

    observed = []

    def query(query, params):
        observed.append(params)
        return [
            {
                "payload": json.dumps(
                    {
                        "type": "graph_complete",
                        "run_id": payload_run,
                        "result": {
                            "run_id": payload_run,
                            "status": "completed",
                            "output": "reply",
                        },
                    }
                )
            }
        ]

    authority = WorkItemA2AAuthority(lambda: SimpleNamespace(query_cypher=query))
    item = {"status": "succeeded", "metadata": {"a2a_run_id": "native-run"}}
    if payload_run == "native-run":
        artifacts = authority._persisted_artifacts("public-work-item", item=item)
        assert artifacts[0].artifact_id == "public-work-item:reply"
        assert artifacts[0].parts[0].text == "reply"
    else:
        with pytest.raises(RuntimeError, match="reply is unavailable"):
            authority._persisted_artifacts("public-work-item", item=item)
    assert observed == [{"run_id": "native-run", "event_type": "graph_complete"}]


@pytest.mark.parametrize("code", ["IDEMPOTENCY_CONFLICT", "UPGRADE_REQUIRED"])
def test_native_admission_conflict_translates_at_a2a_boundary(monkeypatch, code):
    from arbora.a2a.authority import A2AIdempotencyConflict
    from arbora.api.invoke.pipeline import OperationRefused

    def refuse(*args, **kwargs):
        raise OperationRefused(code)

    monkeypatch.setattr(work_items, "submit_work_item_atomic", refuse)
    authority = WorkItemA2AAuthority(
        lambda: object(), task_plan_config=TaskPlanConfig({}, {}, 1, 2)
    )
    expected = (
        A2AIdempotencyConflict if code == "IDEMPOTENCY_CONFLICT" else OperationRefused
    )
    with pytest.raises(expected):
        authority._submit(
            object(),
            task_id="run",
            session=SESSION,
            owner_ref="owner",
            description="input",
            metadata={},
        )


@pytest.mark.asyncio
async def test_native_access_denied_is_unavailable_to_foreign_owner(monkeypatch):
    from epistemic_graph import EngineResponseError

    from arbora.a2a.authority import A2ATaskNotCancelable

    authority = WorkItemA2AAuthority(lambda: object())
    monkeypatch.setattr(authority, "_session", lambda scope: SESSION)
    monkeypatch.setattr(authority, "_engine", lambda: (object(), object()))

    def denied(*args):
        raise EngineResponseError("ACCESS_DENIED", "private native graph detail")

    monkeypatch.setattr(work_items, "get_work_item", denied)
    assert await authority.get("work-item:" + "e" * 64) is None
    with pytest.raises(A2ATaskNotCancelable):
        await authority.cancel("work-item:" + "e" * 64)


def test_owner_lookup_does_not_hide_other_native_failures(monkeypatch):
    from epistemic_graph import EngineResponseError

    authority = WorkItemA2AAuthority(lambda: object())
    monkeypatch.setattr(authority, "_session", lambda scope: SESSION)
    monkeypatch.setattr(authority, "_engine", lambda: (object(), object()))

    def unavailable(*args):
        raise EngineResponseError("INTERNAL", "native failure")

    monkeypatch.setattr(work_items, "get_work_item", unavailable)
    with pytest.raises(EngineResponseError, match="INTERNAL"):
        authority._authorized_item("work-item:" + "e" * 64, "kg:read")
