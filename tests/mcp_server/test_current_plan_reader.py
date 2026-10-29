"""Source-level native reader admission/projection; no installed EG acceptance."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import VerifiedCaller
from arbora.api.ops.runs import read_current_plan
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
from arbora.mcp_server import current_plan_reader as reader_module
from arbora.mcp_server.current_plan_reader import NativeCurrentPlanReader
from tests._support import Actor
from tests.test_native_run_admission import view as admission_view

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.5.1.1")
NOW = datetime(2026, 10, 18, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


class Client:
    def __init__(self):
        self.claims = ContextVar("reader-test-claims", default=None)

    @contextmanager
    def use_verified_context(self, claims):
        token = self.claims.set(claims)
        try:
            yield self
        finally:
            self.claims.reset(token)

    async def _send(self, method, params, graph, **kwargs):
        assert method == "GetRunWorkItem"
        claims = self.claims.get()
        assert params["tenant"] == claims["tenant"]
        assert graph == CONTROL_GRAPH
        value = admission_view()
        value["admission"]["run_id"] = params["run_id"]
        value["admission"]["context"].update(
            subject_id=claims["principal"], tenant_id=claims["tenant"], graph=graph
        )
        return value


def context(principal="caller-a", tenant="tenant-a", client=None):
    claims = {
        "principal": principal,
        "tenant": tenant,
        "scopes": ["kg:read", "work:read"],
        "policy_version": "v1",
        "delegation": [],
    }
    actor = Actor(actor_id=principal, tenant_id=tenant)
    session = SimpleNamespace(
        actor=actor,
        tenant=tenant,
        graph="tenant-content",
        engine_verified_context=lambda: claims,
    )
    caller = VerifiedCaller(
        principal=principal,
        tenant=tenant,
        effective_scopes=frozenset(claims["scopes"]),
        engine_claims=claims,
        principal_kind="human",
        authenticated=True,
        delegated=False,
        credential_kind="session",
        policy_revision="v1",
        session=session,
    )
    return ExecutionContext(
        caller=caller,
        client=client or Client(),
        owner=principal,
        service_identity=False,
        services={"current_plan_reader": NativeCurrentPlanReader()},
    )


def snapshot(**changes):
    return {
        "task_plan": None,
        "run_id": "run-a",
        "revision": 2,
        "observed_at_ms": NOW_MS,
        "expires_at_ms": NOW_MS + 30000,
        "items": [
            {"text": "first", "status": "done"},
            {"text": "second", "status": "pending"},
        ],
        **changes,
    }


async def test_concurrent_reads_bind_exact_caller_claims(monkeypatch):
    client = Client()
    entered = []
    both = asyncio.Event()

    async def native(bound, *, run_id, tenant, graph):
        entered.append((tenant, graph))
        if len(entered) == 2:
            both.set()
        await both.wait()
        assert bound.claims.get()["tenant"] == tenant
        assert bound.claims.get()["principal"] == "caller-" + tenant[-1]
        assert bound.claims.get()["scopes"] == ["kg:read", "work:read"]
        assert graph == CONTROL_GRAPH
        return snapshot()

    monkeypatch.setattr(reader_module, "_native_read", native)
    results = await asyncio.gather(
        *(
            read_current_plan(
                context("caller-" + suffix, "tenant-" + suffix, client),
                "run-a",
                clock=lambda: NOW,
            )
            for suffix in ("a", "b")
        )
    )
    assert all(result.availability == "available" for result in results)
    assert [item.text for item in results[0].snapshot.items] == ["first", "second"]
    assert results[0].snapshot.revision == 2
    assert client.claims.get() is None


@pytest.mark.parametrize(
    "case",
    ["service", "owner", "tenant", "principal", "expired", "graph", "scope", "claims"],
)
async def test_invalid_authority_never_reaches_native_read(monkeypatch, case):
    ctx = context()
    if case == "service":
        ctx = replace(ctx, service_identity=True)
    elif case == "owner":
        ctx = replace(ctx, owner="foreign")
    elif case == "tenant":
        ctx.caller.session.tenant = "foreign"
    elif case == "principal":
        ctx.caller.session.actor.actor_id = "foreign"
    elif case == "expired":
        ctx.caller.session.actor.current = False
    elif case == "graph":
        ctx.caller.session.graph = " control "
    elif case == "scope":
        claims = {**dict(ctx.caller.engine_claims), "scopes": ["work:read"]}
        ctx.caller.session.engine_verified_context = lambda: claims
        ctx = replace(
            ctx,
            caller=replace(
                ctx.caller,
                effective_scopes=frozenset({"work:read"}),
                engine_claims=claims,
            ),
        )
    else:
        ctx.caller.session.engine_verified_context = lambda: {"principal": "changed"}

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid authority reached engine")

    monkeypatch.setattr(reader_module, "_native_read", forbidden)
    monkeypatch.setattr(reader_module, "_native_owner_graph", forbidden)
    with pytest.raises(OperationRefused):
        await read_current_plan(ctx, "run-a", clock=lambda: NOW)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        snapshot(run_id="foreign"),
        snapshot(observed_at_ms=NOW_MS + 1),
        snapshot(expires_at_ms=NOW_MS),
        snapshot(revision=True),
        snapshot(items={}),
        snapshot(items=""),
        snapshot(observed_at_ms=True),
        snapshot(items=[{"text": "bad", "status": "unknown"}]),
        snapshot(expires_at_ms=10**30),
    ],
)
async def test_unavailable_and_malformed_native_values_are_indistinguishable(
    monkeypatch, raw
):
    async def native(*args, **kwargs):
        return raw

    monkeypatch.setattr(reader_module, "_native_read", native)
    result = await read_current_plan(context(), "run-a", clock=lambda: NOW)
    assert result.model_dump() == {"availability": "unavailable", "snapshot": None}


async def test_absent_published_method_is_unavailable(monkeypatch):
    from epistemic_graph.generated import coordination

    monkeypatch.delattr(coordination, "send_get_current_plan", raising=False)
    result = await read_current_plan(context(), "run-a", clock=lambda: NOW)
    assert result.availability == "unavailable"


@pytest.mark.spec("ARBORA-IDENTITY-R004.3")
def test_actual_serving_root_composes_stateless_reader(monkeypatch):
    from arbora.a2a import mcp as a2a_mcp
    from arbora.core.settings import runtime_config
    from arbora.mcp_server import served_invocation, server
    from arbora.mcp_server.backing import server_factory

    captured = {}
    parser = SimpleNamespace(
        parse_known_args=lambda: (SimpleNamespace(transport="stdio"), [])
    )
    monkeypatch.setattr(server_factory, "create_mcp_parser", lambda: parser)
    monkeypatch.setattr(
        server.runtime, "_mint_process_session", lambda transport: object()
    )
    monkeypatch.setattr(a2a_mcp, "_service", lambda: object())

    def compose(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(served_invocation, "served_invocation_from_settings", compose)
    # Patching a dynamic proxy attribute restores a frozen value at teardown.
    # Use the real configuration input and verify later reads remain live.
    with monkeypatch.context() as config_patch:
        config_patch.setenv("GRAPH_SERVICE_ENDPOINTS", "unix:///test-only")
        invocation, session = server._served_invocation()
    monkeypatch.setenv("GRAPH_SERVICE_ENDPOINTS", "tcp://192.0.2.1:80")
    assert runtime_config.graph_service_endpoints == ["tcp://192.0.2.1:80"]
    assert invocation is not None and session is not None
    assert isinstance(
        captured["bindings"]["current_plan_reader"], NativeCurrentPlanReader
    )
    assert vars(captured["bindings"]["current_plan_reader"]) == {}


@pytest.mark.parametrize("outcome", ["snapshot", "null", "malformed"])
async def test_generated_adapter_maps_decoded_result(monkeypatch, outcome):
    from epistemic_graph.generated import ContractViolation, coordination

    captured = []

    async def send(client, params, graph):
        captured.append((params, graph, client.claims.get()))
        return object()

    def decode(result):
        if outcome == "malformed":
            raise ContractViolation("malformed private backend payload")
        if outcome == "null":
            return None
        return SimpleNamespace(model_dump=lambda **kwargs: snapshot())

    monkeypatch.setattr(coordination, "send_get_current_plan", send, raising=False)
    monkeypatch.setattr(coordination, "decode_get_current_plan", decode, raising=False)
    result = await read_current_plan(context(), "run-a", clock=lambda: NOW)
    assert result.availability == (
        "available" if outcome == "snapshot" else "unavailable"
    )
    assert captured[0][0] == {"run_id": "run-a", "tenant": "tenant-a"}
    assert captured[0][1] == CONTROL_GRAPH
    assert captured[0][2]["principal"] == "caller-a"
    assert captured[0][2]["scopes"] == ["kg:read", "work:read"]


async def test_native_authorization_refusal_is_not_hidden_as_missing_plan(monkeypatch):
    from epistemic_graph import EngineResponseError
    from epistemic_graph.generated import coordination

    from arbora.api.errors import EngineRefusal

    ctx = context()
    claims = {**dict(ctx.caller.engine_claims), "scopes": ["kg:read"]}
    ctx.caller.session.engine_verified_context = lambda: claims
    ctx = replace(
        ctx,
        caller=replace(
            ctx.caller, effective_scopes=frozenset({"kg:read"}), engine_claims=claims
        ),
    )

    async def send(client, *args, **kwargs):
        assert client.claims.get()["scopes"] == ["kg:read"]
        raise EngineResponseError("ACCESS_DENIED", "private backend detail")

    monkeypatch.setattr(coordination, "send_get_current_plan", send, raising=False)
    monkeypatch.setattr(
        coordination, "decode_get_current_plan", lambda result: None, raising=False
    )
    with pytest.raises(EngineRefusal) as caught:
        await read_current_plan(ctx, "run-a", clock=lambda: NOW)
    assert ctx.client.claims.get() is None
    assert caught.value.code == "ACCESS_DENIED"


def test_private_executable_plan_is_not_projected():
    raw = snapshot(task_plan={"tasks": [{"prompt": "private executable prompt"}]})
    result = reader_module._project(raw)
    assert result.availability == "available"
    assert "private executable prompt" not in result.model_dump_json()
    assert "task_plan" not in result.model_dump_json()


@pytest.mark.parametrize(
    "field", ["missing", "malformed", "run", "tenant", "owner", "graph", "caller-id"]
)
async def test_unowned_admission_never_reads_plan_payload(monkeypatch, field):
    from epistemic_graph.generated import OpaqueResult, coordination

    value = admission_view()
    value["admission"]["run_id"] = "run-a"
    value["admission"]["context"].update(
        subject_id="caller-a", tenant_id="tenant-a", graph=CONTROL_GRAPH
    )
    if field == "missing":
        value = None
    elif field == "malformed":
        value = {}
    elif field == "run":
        value["admission"]["run_id"] = "foreign"
    elif field == "caller-id":
        value["admission"]["work_item_id"] = "caller-chosen"
    else:
        key = {"tenant": "tenant_id", "owner": "subject_id", "graph": "graph"}[field]
        value["admission"]["context"][key] = "foreign"

    async def lookup(client, params, graph):
        assert client.claims.get()["principal"] == "caller-a"
        assert params == {"tenant": "tenant-a", "run_id": "run-a"}
        assert graph == CONTROL_GRAPH
        return OpaqueResult("GetRunWorkItem", value)

    async def forbidden(*args, **kwargs):
        pytest.fail("unowned admission reached snapshot payload")

    monkeypatch.setattr(coordination, "send_get_run_work_item", lookup)
    monkeypatch.setattr(reader_module, "_native_read", forbidden)
    ctx = context()
    result = await read_current_plan(ctx, "run-a", clock=lambda: NOW)
    assert result.availability == "unavailable"
    assert ctx.client.claims.get() is None


async def test_native_owner_refusal_preserves_code_and_restores_claims(monkeypatch):
    from epistemic_graph import EngineResponseError
    from epistemic_graph.generated import coordination

    from arbora.api.errors import EngineRefusal

    async def denied(client, params, graph):
        raise EngineResponseError("ACCESS_DENIED", "private detail")

    async def forbidden(*args, **kwargs):
        pytest.fail("refused owner reached snapshot payload")

    monkeypatch.setattr(coordination, "send_get_run_work_item", denied)
    monkeypatch.setattr(reader_module, "_native_read", forbidden)
    ctx = context()
    with pytest.raises(EngineRefusal) as error:
        await read_current_plan(ctx, "run-a", clock=lambda: NOW)
    assert error.value.code == "ACCESS_DENIED"
    assert ctx.client.claims.get() is None


@pytest.mark.parametrize(
    "field", ["graph", "owner_graph", "tenant", "principal", "scopes"]
)
def test_request_cannot_choose_native_owner_routing(field):
    from agent_connector_sdk.current_plan import CurrentPlanRequest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CurrentPlanRequest.model_validate({"run_id": "run-a", field: "forged"})


async def test_identity_drift_after_owner_lookup_blocks_payload(monkeypatch):
    ctx = context()

    async def owner(*args, **kwargs):
        ctx.caller.session.actor.current = False
        return CONTROL_GRAPH

    async def forbidden(*args, **kwargs):
        pytest.fail("expired caller reached plan payload")

    monkeypatch.setattr(reader_module, "_native_owner_graph", owner)
    monkeypatch.setattr(reader_module, "_native_read", forbidden)
    with pytest.raises(OperationRefused, match="UNAUTHENTICATED"):
        await read_current_plan(ctx, "run-a", clock=lambda: NOW)
    assert ctx.client.claims.get() is None


async def test_mutable_metadata_cannot_redirect_owner_graph(monkeypatch):
    from epistemic_graph.generated import OpaqueResult, coordination

    value = admission_view()
    value["admission"]["run_id"] = "run-a"
    value["admission"]["context"].update(
        subject_id="caller-a", tenant_id="tenant-a", graph=CONTROL_GRAPH
    )
    value["work_item"]["metadata"] = {"owner_graph": "foreign", "principal": "service"}
    ctx = context()
    expected = dict(ctx.caller.session.engine_verified_context())

    async def lookup(client, params, graph):
        assert client.claims.get() == expected
        assert graph == CONTROL_GRAPH
        return OpaqueResult("GetRunWorkItem", value)

    async def native(client, *, run_id, tenant, graph):
        assert client.claims.get() == expected
        assert (run_id, tenant, graph) == ("run-a", "tenant-a", CONTROL_GRAPH)
        return snapshot()

    monkeypatch.setattr(coordination, "send_get_run_work_item", lookup)
    monkeypatch.setattr(reader_module, "_native_read", native)
    result = await read_current_plan(ctx, "run-a", clock=lambda: NOW)
    assert result.availability == "available"
    assert ctx.client.claims.get() is None
