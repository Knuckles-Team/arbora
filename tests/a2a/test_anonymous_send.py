"""Actual public native codecs and shared text executor over synthetic transports."""

import asyncio
import copy
import time
from dataclasses import replace

import httpx
import msgpack
import pytest
from agent_utilities.runtime import RunBudget, RunPlan, StopRule
from epistemic_graph.client import EngineResponseError, EpistemicGraphClient
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from arbora.a2a.anonymous_send import (
    AnonymousTextPolicy,
    AnonymousTextService,
    anonymous_text_app,
)

pytestmark = [
    pytest.mark.spec("ARBORA-IDENTITY-R004.8.1"),
    pytest.mark.spec("ARBORA-IDENTITY-R004.8.3.1"),
]
ORIGIN = "http://127.0.0.1:8000"


class Writer:
    def get_extra_info(self, name):
        return ("127.0.0.1", 9000) if name == "peername" else None


class Engine:
    def __init__(self):
        self.client = EpistemicGraphClient(
            asyncio.StreamReader(), Writer(), None, "wrong-default", auth_mode="none"
        )
        self.client._send = self.send
        self.client.supports = self.supports
        self.admission = None
        self.receipt = None
        self.calls = []
        self.item = "work-item:" + "a" * 64
        self.status = "ready"
        self.fenced = False
        self.nodes = {}
        self.raw_properties = False

    async def supports(self, name):
        return True

    async def send(self, method, params, graph, *, idempotency_key):
        assert graph == "control"
        self.calls.append(method)
        if method == "SubmitWorkItem":
            request = params["request"]
            created = self.admission is None
            if (
                not created
                and request["command_digest"] != self.admission["command_digest"]
            ):
                raise EngineResponseError("IDEMPOTENCY_CONFLICT")
            if not created and request != self.admission:
                raise EngineResponseError("IDEMPOTENCY_CONFLICT")
            if created:
                self.admission = copy.deepcopy(request)
            return {
                "schema_version": "1",
                "admitted_count": int(created),
                "changed_work_item_ids": [self.item] if created else [],
                "command_digest": request["command_digest"],
                "command_sequence": 1,
                "created": created,
                "replayed": not created,
                "dependency_count": 0,
                "idempotency_key": request["idempotency_key"],
                "max_tenant_in_flight": 1,
                "outbox_id": "fixture-outbox",
                "provenance_refs": [],
                "status": self.status,
                "work_item_id": self.item,
            }
        if method == "ClaimWorkItem":
            if self.status != "ready":
                return {
                    "schema_version": "1",
                    "claimed": False,
                    "reason": "empty",
                    "changed_work_item_ids": [],
                }
            r = params["request"]
            self.status = "running"
            return {
                "schema_version": "1",
                "claimed": True,
                "reason": "claimed",
                "changed_work_item_ids": [self.item],
                "work_item_id": self.item,
                "lease_holder_ref": r["worker_ref"],
                "lease_epoch": 1,
                "fencing_token": 1,
                "attempt": 1,
                "max_attempts": 1,
                "kind": "orchestrator_task",
                "payload_ref": self.admission["input_ref"],
                "lease_expires_at_ms": int(time.time() * 1000) + 10000,
                "tenant_in_flight": 1,
            }
        if method == "CommitWorkItemResult":
            if self.status in {"succeeded", "failed", "cancelled"}:
                return {
                    "status": "noop",
                    "changed_work_item_ids": [],
                    "work_item_id": self.item,
                }
            if self.fenced:
                return {
                    "status": "fenced",
                    "changed_work_item_ids": [],
                    "work_item_id": self.item,
                }
            self.status = params["outcome"]
            self.receipt = {
                "payload": {
                    "completion": params["run_completion"],
                    "tenant": "local",
                    "work_item_id": self.item,
                    "batch_id": "completion-batch",
                    "outcome": self.status,
                    "result_ref": params["result_ref"],
                    "error_ref": None,
                },
                "scope": {
                    "identity_digest": bytes(32),
                    "incarnation_id": bytes(16),
                    "scope": {"kind": "graph", "graph": "control"},
                    "tenant": "local",
                },
                "commit_sequence": 1,
                "ordinal": 0,
                "payload_digest": "a" * 64,
            }
            return {
                "status": self.status,
                "changed_work_item_ids": [self.item],
                "work_item_id": self.item,
                "lease_epoch": 1,
                "fencing_token": 1,
            }
        if method == "GetRunWorkItem":
            if self.admission is None:
                return None
            return {
                "admission": self.admission,
                "work_item": {
                    "work_item_id": self.item,
                    "kind": "orchestrator_task",
                    "status": self.status,
                    "input_ref": self.admission["input_ref"],
                    "metadata": {},
                    "version": 1,
                    "updated_at_ms": 0,
                },
                **({"run_completion": self.receipt} if self.receipt else {}),
            }
        if method == "CancelWorkItem":
            if self.status == "ready":
                self.status = "cancelled"
                return {
                    "status": "cancelled",
                    "changed_work_item_ids": [self.item],
                    "work_item_id": self.item,
                }
            return {
                "status": "in_flight" if self.status == "running" else "noop",
                "changed_work_item_ids": [],
                "work_item_id": self.item,
            }
        if method == "CreateNodeIfAbsent":
            key = params["node_id"]
            if key in self.nodes:
                return False
            self.nodes[key] = msgpack.unpackb(params["properties_msgpack"], raw=False)
            return True
        if method == "GetNodeProperties":
            value = copy.deepcopy(self.nodes.get(params["node_id"]))
            return (
                msgpack.packb(value, use_bin_type=True)
                if self.raw_properties
                else value
            )
        raise AssertionError(method)


def setup(handler=None):
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "hello"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    client = AsyncOpenAI(
        api_key="inert-test-only",  # sanitizer:ignore - inert synthetic provider fixture
        base_url="http://fixture.invalid/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler or reply)),
        max_retries=0,
        timeout=2,
    )
    model = OpenAIChatModel("fixture", provider=OpenAIProvider(openai_client=client))
    plan = RunPlan(
        model=model,
        execution_constraint="text-only",
        steps=("placeholder",),
        budget=RunBudget(
            request_limit=1,
            tool_calls_limit=0,
            total_tokens_limit=20,
            timeout_seconds=2,
            max_attachments=0,
            max_attachment_bytes=0,
            max_concurrent_requests=1,
        ),
        stop=StopRule(max_steps=1),
    )
    plan = replace(
        plan,
        harness=replace(plan.harness, retries=0, model_settings={"max_tokens": 10}),
    )
    engine = Engine()
    service = AnonymousTextService(
        mode="none",
        policy=AnonymousTextPolicy(
            ORIGIN,
            "control",
            "policy-1",
            "fixture",
            operations=frozenset({"message/send"}),
        ),
        client=engine.client,
        plan=plan,
    )
    return engine, service, client, calls


async def post(service, *, text="hello", key="message-1", origin=ORIGIN):
    transport = httpx.ASGITransport(
        app=anonymous_text_app(service), client=("127.0.0.1", 10000)
    )
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        return await client.post(
            "/a2a",
            headers={"Origin": origin, "X-Arbora-Anonymous": "1"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": key,
                        "parts": [{"kind": "text", "text": text}],
                    }
                },
            },
        )


@pytest.mark.asyncio
async def test_actual_native_codec_harness_and_a2a_reply_replay():
    engine, service, client, calls = setup()
    try:
        response = await post(service)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert (
            result["status"]["state"] == "completed"
            and result["artifacts"][0]["parts"][0]["text"] == "hello"
        )
        replay = await post(service)
        assert replay.status_code == 200, replay.text
        assert replay.json()["result"]["artifacts"] == result["artifacts"]
        assert replay.json()["result"]["metadata"]["durableAnswerAvailable"] is True
        assert len(calls) == 1 and engine.calls.count("ClaimWorkItem") == 1
        assert engine.admission["context"]["authentication_method"] == "none"
        assert engine.admission["context"]["scopes"] == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_changed_input_or_fenced_result_never_claims_success():
    engine, service, client, calls = setup()
    try:
        engine.fenced = True
        assert (await post(service)).status_code == 503
        assert (await post(service, text="changed")).status_code == 503
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_hostile_origin_has_no_native_or_model_effect():
    engine, service, client, calls = setup()
    try:
        assert (await post(service, origin="http://evil.invalid")).status_code == 403
        assert engine.calls == calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_recreated_service_recovers_native_answer_and_refuses_tamper():
    engine, service, client, calls = setup()
    try:
        first = await post(service)
        rebuilt = AnonymousTextService(
            mode="none", policy=service.policy, client=engine.client, plan=service._plan
        )
        replay = await post(rebuilt)
        assert (
            replay.json()["result"]["artifacts"] == first.json()["result"]["artifacts"]
        )
        assert len(calls) == 1
        next(iter(engine.nodes.values()))["payload"] = "forged"
        assert (await post(rebuilt)).status_code == 503
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_revoke_drains_model_and_closes_exact_native_lease():
    entered, drained = asyncio.Event(), asyncio.Event()

    async def held(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    engine, service, client, _ = setup(held)
    try:
        pending = asyncio.create_task(post(service))
        await entered.wait()
        service.revoke()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert drained.is_set() and not service._active
        assert engine.status == "cancelled"
        assert engine.calls.count("CommitWorkItemResult") >= 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_default_empty_policy_never_reaches_native_or_model():
    engine, service, client, calls = setup()
    try:
        blocked = AnonymousTextService(
            mode="none",
            policy=replace(service.policy, operations=frozenset()),
            client=engine.client,
            plan=service._plan,
        )
        assert (await post(blocked)).status_code == 403
        assert engine.calls == calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_raw_native_properties_and_plan_change_refusal():
    engine, service, client, calls = setup()
    try:
        engine.raw_properties = True
        assert (await post(service)).status_code == 200
        assert (await post(service)).status_code == 200
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_plan_mutation_during_claim_has_no_model_call():
    engine, service, client, calls = setup()
    original = engine.send

    async def mutate(method, params, graph, *, idempotency_key):
        value = await original(method, params, graph, idempotency_key=idempotency_key)
        if method == "ClaimWorkItem":
            service._plan.harness.model_settings["max_tokens"] = 15
        return value

    engine.client._send = mutate
    try:
        assert (await post(service)).status_code == 403
        assert calls == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_withdrawal_after_submit_cancels_ready_native_row():
    engine, service, client, calls = setup()
    original = engine.send

    async def withdraw(method, params, graph, *, idempotency_key):
        value = await original(method, params, graph, idempotency_key=idempotency_key)
        if method == "SubmitWorkItem":
            service._revoked = True
        return value

    engine.client._send = withdraw
    try:
        assert (await post(service)).status_code == 403
        assert calls == [] and engine.status == "cancelled"
        assert "ClaimWorkItem" not in engine.calls
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("denied"),
        EngineResponseError("ACCESS_DENIED"),
        ValueError("bad codec"),
    ],
)
async def test_submit_refusal_never_reconciles_to_existing_answer(failure):
    engine, service, client, calls = setup()
    try:
        assert (await post(service)).status_code == 200
        original = engine.send
        reads = 0

        async def refuse(method, params, graph, *, idempotency_key):
            nonlocal reads
            if method == "GetRunWorkItem":
                reads += 1
                if reads == 1:
                    return None
            if method == "SubmitWorkItem":
                raise failure
            return await original(
                method, params, graph, idempotency_key=idempotency_key
            )

        engine.client._send = refuse
        response = await post(service)
        assert response.status_code != 200
        assert reads == 1 and len(calls) == 1
        assert "hello" not in response.text
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("input_ref", "sha256:" + "b" * 64),
        ("max_attempts", 2),
        ("policy_digest", "b" * 64),
        ("metadata", {"other": "value"}),
    ],
)
async def test_same_digest_altered_original_admission_is_refused(field, value):
    engine, service, client, calls = setup()
    try:
        assert (await post(service)).status_code == 200
        engine.admission[field] = value
        response = await post(service)
        assert response.status_code == 503
        assert "hello" not in response.text and len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,accepted",
    [("\x00" * 60000, False), ("😀" * 65000, True), ("😀" * 65536, False)],
    ids=["escaped-control", "multibyte-accepted", "multibyte-overflow"],
)
async def test_encoded_answer_bound_matches_raw_recovery(text, accepted):
    async def reply(request):
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            },
        )

    engine, service, client, calls = setup(reply)
    engine.raw_properties = True
    try:
        first = await post(service)
        if accepted:
            assert first.status_code == 200, first.text
            second = await post(service)
            assert second.status_code == 200, second.text
            assert (
                first.json()["result"]["artifacts"]
                == second.json()["result"]["artifacts"]
            )
        else:
            assert first.status_code != 200
            assert "CreateNodeIfAbsent" not in engine.calls
            assert engine.status == "failed"
            assert engine.receipt["payload"]["result_ref"] is None
    finally:
        await client.close()
