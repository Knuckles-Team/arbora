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
from arbora.mcp_server import current_plan_reader as reader_module
from arbora.mcp_server.current_plan_reader import NativeCurrentPlanReader
from tests._support import Actor

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
        graph="control",
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


def test_actual_serving_root_composes_stateless_reader(monkeypatch):
    from arbora.a2a import mcp as a2a_mcp
    from arbora.core.settings import runtime_config
    from arbora.mcp_server import served_invocation, server
    from arbora.mcp_server.backing import server_factory

    captured = {}
    monkeypatch.setattr(
        runtime_config, "graph_service_endpoints", ["unix:///test-only"]
    )
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
    invocation, session = server._served_invocation()
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
    assert captured[0][1] == "control"
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
