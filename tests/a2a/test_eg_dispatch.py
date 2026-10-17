"""Fixture-operation execution through real A2A, invoke, runtime and EG dispatch.

This does not admit the unavailable full production registry or qualify a live
engine. Only the provider's final _send transport boundary is recorded.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from epistemic_graph import EngineResponseError
from pydantic import BaseModel, ConfigDict, RootModel

from graph_os.api.errors import EngineRefusal
from graph_os.api.invoke import bootstrap
from graph_os.api.invoke.executor import ExecutionContext
from graph_os.api.invoke.pipeline import OperationRefused
from graph_os.api.mcp.caller import caller_from_session
from graph_os.api.policy.eunomia import PolicyGate
from graph_os.api.registry import (
    AuditClass,
    Effect,
    EgMethod,
    Idempotency,
    Surface,
    Verb,
)
from graph_os.security import request_identity
from tests.a2a._support import authenticated_operation_app
from tests.a2a.test_operation_bridge import DenyPolicy, post, session
from tests.a2a.test_request_session_authority import fixture as request_fixture
from tests.api.test_invoke_contract import operation, setup_services

pytestmark = pytest.mark.spec("GRAPHOS-OPS-R009")

#: The engine's per-tenant graph name for tenant ``tenant:one``.
_TENANT_GRAPH = "tenant__tenant_one____commons__"


class ReadParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordingClient:
    def __init__(self) -> None:
        self.claims: Any = None
        self.sent: list[Any] = []
        self.acquired: list[Any] = []
        self.error: Exception | None = None

    @contextmanager
    def use_verified_context(self, claims: Any) -> Any:
        previous = self.claims
        self.claims = claims
        try:
            yield self
        finally:
            self.claims = previous

    async def _send(
        self, method: str, params: Any, graph: str, *, idempotency_key: Any = None
    ) -> Any:
        self.sent.append((method, params, graph, idempotency_key, self.claims))
        if self.error is not None:
            raise self.error
        return 47


def fixture(monkeypatch: pytest.MonkeyPatch, failure: str = "") -> Any:
    request_fixture(monkeypatch, failure=failure)
    credential_owner = request_identity.actor_from_bearer_token

    async def read_credential_owner(credential: str) -> Any:
        actor = await credential_owner(credential)
        # Synthetic issuer grants the provider method's actual node:read scope;
        # AU's unchanged public minter projects these validated roles.
        return replace(
            actor,
            roles=actor.roles + (() if failure == "scope" else ("node:read",)),
        )

    monkeypatch.setattr(
        request_identity, "actor_from_bearer_token", read_credential_owner
    )
    client = RecordingClient()
    callers: list[Any] = []
    verify = bootstrap.verify_current_from_session

    async def current(caller: Any) -> Any:
        callers.append(caller)
        result = await verify(caller)
        return (
            replace(result, principal="person:substituted")
            if failure == "mismatched"
            else result
        )

    async def connected(_context: Any, _graph: str) -> Any:
        client.acquired.append((_context, _graph))
        return client

    async def service_claims(_tenant: Any, _scopes: Any) -> Any:
        raise AssertionError("caller-only read cannot acquire service authority")

    def connect_context(tenant: str) -> Any:
        return SimpleNamespace(tenant=tenant)

    pool: Any = SimpleNamespace(connected_client=connected)
    monkeypatch.setattr(bootstrap, "verify_current_from_session", current)
    runtime = bootstrap.build_operation_runtime(
        pool=pool,
        graph="fixture:wrong-graph" if failure == "graph-mismatched" else _TENANT_GRAPH,
        connect_context=connect_context,
        service_scopes=frozenset(),
        service_principal="service:fixture",
        service_claims=service_claims,
    )
    op = operation(
        id="fixture.eg_read",
        verb=Verb.ASK,
        params=ReadParams,
        result=RootModel[int],
        binding=EgMethod(service="NodeCount", op="NodeCount"),
        scopes=frozenset({"node:read"}),
        effect=Effect.READ,
        audit=AuditClass.NONE,
        idempotency=Idempotency.NATURAL,
        surfaces=frozenset({Surface.A2A}),
    )
    changes = (
        {"policy_gate": PolicyGate("remote", DenyPolicy())}
        if failure == "policy"
        else {}
    )
    services, _runtime, _audit, _journal, _leases = setup_services(op, **changes)
    services = replace(services, runtime=runtime)
    app = authenticated_operation_app(services)
    return app, runtime, client, callers, op


def params(**changes: Any) -> dict[str, Any]:
    return {
        "op": "fixture.eg_read",
        "params": {},
        "idempotencyKey": "read:one",
        **changes,
    }


def test_a2a_read_uses_real_runtime_and_provider_send_with_bound_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _runtime, client, callers, _op = fixture(monkeypatch)
    response = post(app, params=params())
    assert response.status_code == 200
    assert response.json()["result"]["result"] == 47
    assert len(client.sent) == 1
    method, arguments, graph, key, claims = client.sent[0]
    assert (method, arguments, key) == ("NodeCount", {}, "read:one")
    assert graph == callers[0].session.graph and graph
    assert claims["principal"] == callers[0].principal
    assert claims["tenant"] == callers[0].tenant
    assert frozenset(claims["scopes"]) == callers[0].effective_scopes
    assert client.claims is None


@pytest.mark.parametrize(
    "failure", ["credential", "scope", "policy", "expired", "mismatched"]
)
def test_denied_request_never_reaches_provider_send(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    app, _runtime, client, _callers, _op = fixture(monkeypatch, failure)
    response = post(app, params=params())
    assert response.status_code in {401, 403}
    assert client.sent == []


def test_closed_read_schema_rejects_graph_retargeting_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _runtime, client, _callers, _op = fixture(monkeypatch)
    response = post(app, params=params(params={"graph": "tenant:foreign"}))
    assert response.status_code == 400
    assert client.sent == []


def context(
    client: RecordingClient, *, graph: Any = "tenant:bound", **changes: Any
) -> ExecutionContext:
    bound = replace(session(frozenset({"node:read"})), graph=graph)
    caller = caller_from_session(bound, credential_kind="bearer")
    values: dict[str, Any] = dict(
        caller=caller,
        client=client,
        owner=caller.principal,
        service_identity=False,
        services={},
        idempotency_key="direct:one",
    )
    values.update(changes)
    return ExecutionContext(**values)


@pytest.mark.parametrize(
    "binding,changes,code",
    [
        (SimpleNamespace(service="NodeCount", op="NodeCount"), {}, "UNAVAILABLE"),
        (EgMethod(service="NodeCount", op="Ping"), {}, "UNAVAILABLE"),
        (
            EgMethod(service="UnsupportedFixtureMethod", op="UnsupportedFixtureMethod"),
            {},
            "UNAVAILABLE",
        ),
        (
            EgMethod(service="NodeCount", op="NodeCount"),
            {"service_identity": True},
            "PRINCIPAL_NOT_ALLOWED",
        ),
        (
            EgMethod(service="NodeCount", op="NodeCount"),
            {"owner": "person:substituted"},
            "UNAUTHENTICATED",
        ),
    ],
)
def test_dispatch_refuses_unsupported_bindings_and_service_identity(
    binding: Any, changes: Any, code: str
) -> None:
    client = RecordingClient()
    with pytest.raises(OperationRefused) as error:
        asyncio.run(
            bootstrap.dispatch_eg_method(binding, {}, context(client, **changes))
        )
    assert error.value.code == code
    assert client.sent == []


@pytest.mark.parametrize("graph", [None, "", " tenant:bound "])
def test_dispatch_refuses_missing_or_invalid_session_graph(graph: Any) -> None:
    client = RecordingClient()
    with pytest.raises(OperationRefused) as error:
        asyncio.run(
            bootstrap.dispatch_eg_method(
                EgMethod(service="NodeCount", op="NodeCount"),
                {},
                context(client, graph=graph),
            )
        )
    assert error.value.code == "UNAUTHENTICATED"
    assert client.sent == []


def test_provider_payload_does_not_change_outer_bound_graph() -> None:
    client = RecordingClient()
    result = asyncio.run(
        bootstrap.dispatch_eg_method(
            EgMethod(service="NodeCount", op="NodeCount"),
            {"graph": "payload:value"},
            context(client),
        )
    )
    assert result == 47
    assert client.sent[0][:4] == (
        "NodeCount",
        {"graph": "payload:value"},
        "tenant:bound",
        "direct:one",
    )


def test_runtime_preserves_engine_refusal_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _app, runtime, client, _callers, op = fixture(monkeypatch)
    client.error = EngineResponseError("ACCESS_DENIED", "private diagnostic")
    with pytest.raises(EngineRefusal) as error:
        asyncio.run(
            runtime.dispatch(
                op,
                {},
                context(
                    client,
                    graph=_TENANT_GRAPH,
                ),
            )
        )
    assert error.value.code == "ACCESS_DENIED"
    assert len(client.sent) == 1


def test_configured_graph_mismatch_refuses_before_client_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _runtime, client, _callers, _op = fixture(monkeypatch, "graph-mismatched")
    response = post(app, params=params())
    assert response.status_code == 401
    assert client.acquired == []
    assert client.sent == []
