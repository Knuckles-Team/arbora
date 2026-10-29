"""Served R043.6 proof over the real Arbora HTTP invocation composition."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from arbora.api.mcp.discovery import describe_op
from arbora.api.ops.query import operations
from arbora.api.ops.query_federation import (
    ForeignReadDescriptor,
    NaturalLanguageResult,
    PreparedFederatedQuery,
)
from arbora.core.settings import runtime_config
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.security import request_identity
from tests.test_served_operation_api import FakeClient, FakePool, _actor, _call, _value

_DIGEST = "0" * 64


class _FederatedClient(FakeClient):
    async def _send(
        self, method: str, params: Any, graph: str | None, **kwargs: Any
    ) -> Any:
        self.sent.append((method, params, graph))
        if method == "Uql":
            return {
                "Rows": {
                    "rows": [
                        {"id": "native:deployment"},
                        {"id": "virtual:issue"},
                    ],
                    "columns": [],
                    "warnings": [],
                },
                # These are producer-owned origin counters.  They are not
                # inferred from the returned row count.
                "federation": {"native_count": 1, "virtual_count": 1},
            }
        return await super()._send(method, params, graph, **kwargs)


class _NativeOnlyClient(FakeClient):
    """Return the unchanged EG rows-only body when no planner is composed."""

    async def _send(
        self, method: str, params: Any, graph: str | None, **kwargs: Any
    ) -> Any:
        if method == "NlQuery":
            self.sent.append((method, params, graph))
            return [["native:deployment", 0.75], ["native:service", None]]
        return await super()._send(method, params, graph, **kwargs)


class _Planner:
    async def prepare(self, **_: Any) -> PreparedFederatedQuery:
        return PreparedFederatedQuery(
            uql="FOREIGN 'source:gitlab' |> LIMIT 1",
            params={},
            plan_digest=_DIGEST,
            query_digest=_DIGEST,
            sources=(
                ForeignReadDescriptor(
                    source_id="source:gitlab",
                    limits={
                        "chunk_bytes": 1024,
                        "chunk_rows": 10,
                        "deadline_ms": 1000,
                        "max_bytes": 4096,
                        "max_rows": 10,
                    },
                ),
            ),
        )


class _Binding:
    source = SimpleNamespace(
        connector="gitlab",
        contract=SimpleNamespace(
            contract_iri="iri:contract:gitlab", operation="issues.list"
        ),
        mapping=SimpleNamespace(mapping_id="iri:mapping:gitlab"),
    )

    async def read(self, **kwargs: Any) -> Any:
        query_result = await kwargs["correlate"](
            {"execution_id": 7, "query_digest": _DIGEST}
        )
        return SimpleNamespace(
            ok=True,
            query_result=query_result,
            rows=1,
            pages=1,
            truncated=False,
        )


def _served_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, _FederatedClient]:
    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "policy:fixture")
    monkeypatch.setattr(
        runtime_config, "auth_jwt_jwks_uri", "https://identity.invalid/jwks"
    )
    base_actor = _actor()
    actor = replace(base_actor, roles=(*base_actor.roles, "query:nl"))

    async def credential_owner(credential: str) -> Any:
        assert credential == "fixture-verified"
        return actor

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    client = _FederatedClient()
    process = request_identity.mint_graph_session(actor)
    invocation = compose_served_invocation(
        pool=FakePool(client),
        graph=process.graph,
        process_session=lambda: process,
        bindings={
            "nl_federation_planner": _Planner(),
            "fleet_source_binding": lambda *_args, **_kwargs: _Binding(),
        },
        fleet_effect=refused_fleet_effect,
    )
    app = FastAPI()
    invocation.mount_http(app, prefix="/api")
    return app, client


def _served_native_app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, _NativeOnlyClient]:
    """Compose the served path without the optional native planner port."""

    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "policy:fixture")
    monkeypatch.setattr(
        runtime_config, "auth_jwt_jwks_uri", "https://identity.invalid/jwks"
    )
    base_actor = _actor()
    actor = replace(base_actor, roles=(*base_actor.roles, "query:nl"))

    async def credential_owner(credential: str) -> Any:
        assert credential == "fixture-verified"
        return actor

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    client = _NativeOnlyClient()
    process = request_identity.mint_graph_session(actor)
    invocation = compose_served_invocation(
        pool=FakePool(client),
        graph=process.graph,
        process_session=lambda: process,
        bindings={},
        fleet_effect=refused_fleet_effect,
    )
    app = FastAPI()
    invocation.mount_http(app, prefix="/api")
    return app, client


@pytest.mark.spec("ARBORA-FLEET-R043.3")
def test_nl_question_spans_native_facts_and_live_connector_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The served query returns one typed projection over native and virtual rows."""
    app, client = _served_app(monkeypatch)

    response = _call(app, "query.nl", {"text": "which deployment has issues"})
    value = _value(response)

    assert value["rows"] == [
        {"id": "native:deployment"},
        {"id": "virtual:issue"},
    ]
    assert value["native_count"] == 1
    assert value["virtual_count"] == 1
    assert value["sources"] == [
        {
            "connector": "gitlab",
            "contract_iri": "iri:contract:gitlab",
            "mapping_iri": "iri:mapping:gitlab",
        }
    ]
    assert [method for method, _params, _graph in client.sent] == ["Uql"]

    nl = next(op for op in operations() if op.id == "query.nl")
    descriptor = describe_op(nl)
    variants = descriptor["result_schema"]["anyOf"]
    assert any(variant.get("type") == "array" for variant in variants)
    view_ref = next(
        variant["$ref"]
        for variant in variants
        if "$ref" in variant and variant["$ref"].endswith("FederatedAnswerView")
    )
    properties = descriptor["result_schema"]["$defs"][view_ref.rsplit("/", 1)[-1]][
        "properties"
    ]
    assert {"rows", "sources", "native_count", "virtual_count"} <= set(properties)


@pytest.mark.spec("ARBORA-OPS-R018.2.1")
def test_nl_question_without_planner_preserves_legacy_native_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-planner served path keeps EG's rows-only response valid."""

    app, client = _served_native_app(monkeypatch)

    response = _call(app, "query.nl", {"text": "which deployment failed"})
    value = _value(response)

    assert value == [["native:deployment", 0.75], ["native:service", None]]
    validated = NaturalLanguageResult.model_validate(value)
    assert validated.root == [("native:deployment", 0.75), ("native:service", None)]
    assert len(client.sent) == 1
    method, params, graph = client.sent[0]
    assert method == "NlQuery"
    assert params == {"text": "which deployment failed", "graph": graph}
    assert isinstance(graph, str) and graph
