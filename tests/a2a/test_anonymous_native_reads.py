"""Real public EG client and codec over a synthetic native response transport."""

import asyncio
import copy

import pytest
from epistemic_graph.client import EpistemicGraphClient

from arbora.a2a.anonymous import AnonymousCaller
from arbora.a2a.anonymous_native import EgAnonymousTaskReadPort

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.7.3")
RUN = "a2a-" + "a" * 64
CONTEXT = "a2a-context-" + "b" * 64


class LocalWriter:
    def get_extra_info(self, name):
        return ("127.0.0.1", 9999) if name == "peername" else None


def original_admission():
    return {
        "schema_version": "1",
        "context": {
            "schema_version": "2",
            "request_id": "request-fixture",
            "subject_id": "not-used-as-authority",
            "tenant_id": "local",
            "agent_id": "not-used-as-authority",
            "scopes": [],
            "audience": "engine-fixture",
            "authentication_method": "local_process",
            "policy_version": "policy-fixture",
            "graph": "control",
            "placement_epoch": None,
            "trace_id": "trace-fixture",
            "issued_at_ms": 0,
            "expires_at_ms": 1,
        },
        "run_id": RUN,
        "work_item_id": None,
        "idempotency_key": "fixture",
        "command_digest": "a" * 64,
        "kind": "orchestrator_task",
        "priority": 0,
        "depends_on": [],
        "input_ref": "input:fixture",
        "policy_digest": "policy:fixture",
        "catalog_digest": "catalog:fixture",
        "model_digest": "model:fixture",
        "max_attempts": 1,
        "deadline_unix": None,
        "metadata": {"a2a_context_id": CONTEXT},
        "provenance_refs": [],
        "max_tenant_in_flight": 1,
    }


def response():
    return {
        "admission": original_admission(),
        "work_item": {
            "work_item_id": "work-item:" + "c" * 64,
            "kind": "orchestrator_task",
            "status": "succeeded",
            "input_ref": "input:fixture",
            "metadata": {},
            "version": 1,
            "updated_at_ms": 0,
        },
    }


def setup(value):
    client = EpistemicGraphClient(
        asyncio.StreamReader(),
        LocalWriter(),
        None,
        "different-client-default",
        auth_mode="none",
    )
    sent = []

    async def send(method, params, graph, *, idempotency_key):
        sent.append((method, params, graph))
        return copy.deepcopy(value)

    async def supports(method):
        return method == "GetRunWorkItem"

    client._send = send
    client.supports = supports
    port = EgAnonymousTaskReadPort(client, graph="control")
    caller = AnonymousCaller("local", "policy-1", RUN, "descriptive-namespace")
    return client, port, caller, sent


@pytest.mark.asyncio
async def test_real_get_run_codec_preserves_native_binding_not_metadata_claims():
    _, port, caller, sent = setup(response())
    result = await port.get(caller)
    assert sent == [("GetRunWorkItem", {"tenant": "local", "run_id": RUN}, "control")]
    assert result.run_id == RUN
    assert result.context_id == CONTEXT
    assert result.tenant == "local"
    assert result.authority_mode == "none"
    assert "not-used-as-authority" not in result.owner_ref


@pytest.mark.asyncio
async def test_foreign_owner_none_has_no_getworkitem_fallback():
    _, port, caller, sent = setup(None)
    assert await port.get(caller) is None
    assert len(sent) == 1 and sent[0][0] == "GetRunWorkItem"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("run_id", "foreign"), ("work_item_id", "caller-selected")]
)
async def test_original_identity_mismatch_refuses_in_real_client(field, value):
    payload = response()
    payload["admission"][field] = value
    _, port, caller, _ = setup(payload)
    with pytest.raises(PermissionError):
        await port.get(caller)


@pytest.mark.asyncio
async def test_wrong_graph_refuses_without_inferred_recipient():
    payload = response()
    payload["admission"]["context"]["graph"] = "other"
    _, port, caller, _ = setup(payload)
    with pytest.raises(PermissionError):
        await port.get(caller)


@pytest.mark.asyncio
async def test_mode_change_during_native_request_refuses():
    client, port, caller, _ = setup(response())
    original = client._send

    async def changes(*args, **kwargs):
        result = await original(*args, **kwargs)
        client._auth_mode = "authenticated"
        return result

    client._send = changes
    with pytest.raises(PermissionError):
        await port.get(caller)


@pytest.mark.asyncio
async def test_unadvertised_operation_is_unavailable_without_native_call():
    client, port, caller, sent = setup(response())

    async def unsupported(method):
        return False

    client.supports = unsupported
    with pytest.raises(RuntimeError):
        await port.get(caller)
    assert sent == []


@pytest.mark.asyncio
async def test_real_http_service_and_generated_native_sender_compose():
    import httpx

    from arbora.a2a.anonymous import (
        AnonymousReadPolicy,
        AnonymousTaskReadOwner,
        create_anonymous_a2a_application,
    )
    from arbora.a2a.service import A2AService

    _, port, _, sent = setup(response())
    owner = AnonymousTaskReadOwner(
        mode="none",
        policy=AnonymousReadPolicy(
            "http://127.0.0.1:8080", "local", "revision-1", frozenset({"tasks/get"})
        ),
        work_items=port,
    )
    app = create_anonymous_a2a_application(
        owner=owner, service=A2AService(authority=owner, router=None)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8080"
    ) as client:
        result = await client.post(
            "/a2a",
            headers={"Origin": "http://127.0.0.1:8080", "X-Arbora-Anonymous": "1"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tasks/get",
                "params": {"id": RUN},
            },
        )
    assert result.status_code == 200, result.text
    assert result.json()["result"]["id"] == RUN
    assert sent == [("GetRunWorkItem", {"tenant": "local", "run_id": RUN}, "control")]
    assert owner._active == 0
