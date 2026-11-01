"""Query contract and real caller dispatcher acceptance (no live engine)."""

from __future__ import annotations

import asyncio
import json
from importlib import resources
from typing import Any

import pytest
from medulla import EngineResponseError
from pydantic import ValidationError

from arbora.api.invoke.bootstrap import dispatch_eg_method
from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.query import NaturalLanguageParams, operations
from arbora.api.ops.query_federation import NaturalLanguageResult
from arbora.api.registry import EgMethod, EgSchemaRef
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
from tests._support import Actor
from tests.api.test_invoke_bootstrap import FakeSession, _caller


class RecordingClient:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.error: Exception | None = None

    async def _send(
        self, method: str, params: Any, graph: str, *, idempotency_key: Any = None
    ) -> int:
        self.sent.append((method, params, graph))
        if self.error is not None:
            raise self.error
        return 47


class QuerySession(FakeSession):
    graph = "tenant:bound"


def context(client: RecordingClient) -> ExecutionContext:
    session = QuerySession(actor=Actor(), tenant="tenant:a")
    caller = _caller(
        session,
        engine_claims=session.engine_verified_context(),
        policy_revision="policy:1",
    )
    return ExecutionContext(
        caller=caller,
        client=client,
        owner=caller.principal,
        service_identity=False,
        services={},
    )


pytestmark = pytest.mark.spec("ARBORA-OPS-R018.2.1")


def test_query_bindings_match_actual_provider_authority_and_inner_schema() -> None:
    ops = operations()
    contract = resources.files("medulla").joinpath("contract")
    methods = json.loads(contract.joinpath("methods.json").read_text())["methods"]
    for op in ops:
        binding = op.binding
        assert isinstance(binding, EgMethod)
        method = next(row for row in methods if row["id"] == binding.service)
        assert op.scopes == frozenset({method["policy"]["authz_action"]})
        assert method["is_wire_callable"] is True
        assert method["policy"]["mutates"] is False
    assert [(op.id, op.scopes, op.binding) for op in ops] == [
        ("query.uql", frozenset({"query:unified"}), EgMethod(service="Uql", op="Uql")),
        (
            "query.nl",
            frozenset({"query:nl"}),
            EgMethod(service="NlQuery", op="NlQuery"),
        ),
    ]
    ref = ops[0].params
    assert isinstance(ref, EgSchemaRef)
    document = json.loads(
        resources.files("medulla")
        .joinpath("contract/schemas/method.request.json")
        .read_text()
    )
    selected: Any = document
    for key in ref.path.split("#/")[1].split("/"):
        selected = selected[key]
    assert selected["required"] == ["text"]
    # Engine 2.27.10 adds prepare and foreign_execution to the request.
    assert set(selected["properties"]) == {
        "text",
        "params",
        "prepare",
        "foreign_execution",
    }
    assert selected["additionalProperties"] is False
    nl = next(op for op in ops if op.id == "query.nl")
    assert nl.result is NaturalLanguageResult


@pytest.mark.parametrize(
    "extra", [{"graph": "foreign"}, {"nl": True}, {"query": "text"}]
)
def test_nl_input_cannot_override_graph_or_use_legacy_uql_fields(
    extra: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        NaturalLanguageParams.model_validate({"text": "why", **extra})


@pytest.mark.parametrize(
    "method,payload",
    [("Uql", {"text": "MATCH ()", "params": {}}), ("NlQuery", {"text": "why"})],
)
def test_query_dispatch_uses_verified_graph(
    method: str, payload: dict[str, Any]
) -> None:
    client = RecordingClient()
    result = asyncio.run(
        dispatch_eg_method(
            EgMethod(service=method, op=method), payload, context(client)
        )
    )
    assert result == 47
    expected = {**payload, "graph": "tenant:bound"} if method == "NlQuery" else payload
    assert client.sent[0][:3] == (method, expected, "tenant:bound")
    assert "graph" not in payload


def test_nl_dispatch_refuses_graph_retargeting_before_send() -> None:
    client = RecordingClient()
    with pytest.raises(OperationRefused) as error:
        asyncio.run(
            dispatch_eg_method(
                EgMethod(service="NlQuery", op="NlQuery"),
                {"text": "why", "graph": "foreign"},
                context(client),
            )
        )
    assert error.value.code == "INVALID_ARGUMENT"
    assert client.sent == []


@pytest.mark.parametrize(
    "tenant,allowed",
    [("tenant:a", True), ("tenant:other", False), (None, False)],
)
def test_owner_read_dispatch_binds_wire_tenant_to_verified_caller(
    tenant: str | None, allowed: bool
) -> None:
    client = RecordingClient()
    payload = {"run_id": "run:one"}
    if tenant is not None:
        payload["tenant"] = tenant
    if not allowed:
        with pytest.raises(OperationRefused) as error:
            asyncio.run(
                dispatch_eg_method(
                    EgMethod(service="GetRunWorkItem", op="GetRunWorkItem"),
                    payload,
                    context(client),
                )
            )
        assert error.value.code == "INVALID_ARGUMENT"
        assert error.value.details == {"field": "tenant"}
        assert client.sent == []
        return

    assert (
        asyncio.run(
            dispatch_eg_method(
                EgMethod(service="GetRunWorkItem", op="GetRunWorkItem"),
                payload,
                context(client),
            )
        )
        == 47
    )
    assert client.sent[0][:3] == ("GetRunWorkItem", payload, CONTROL_GRAPH)


def test_nl_planner_unavailable_propagates_without_uql_fallback() -> None:
    client = RecordingClient()
    client.error = EngineResponseError("UNAVAILABLE", "planner unavailable")
    with pytest.raises(EngineResponseError):
        asyncio.run(
            dispatch_eg_method(
                EgMethod(service="NlQuery", op="NlQuery"),
                {"text": "why"},
                context(client),
            )
        )
    assert [sent[0] for sent in client.sent] == ["NlQuery"]


@pytest.mark.parametrize(
    "scope,allowed", [("query:unified", False), ("query:nl", True)]
)
def test_nl_invocation_requires_its_distinct_permission(
    scope: str, allowed: bool
) -> None:
    from tests.api._support import make_verified_caller
    from tests.api.test_invoke_contract import run, setup_services

    op = operations()[1]
    services, runtime, _, _, _ = setup_services(op)

    async def reply() -> dict[str, Any]:
        return {"rows": []}

    runtime.on_dispatch = reply
    result = run(
        services,
        op_id=op.id,
        params={"text": "why"},
        supplied=make_verified_caller(scope)(),
    )
    assert result.code == ("OK" if allowed else "SCOPE_REQUIRED")
    assert bool(runtime.calls) is allowed
