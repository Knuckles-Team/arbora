"""Focused tests for the composed R043.6 Arbora caller seam."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.query_federation import (
    NativeNlQueryPlanPlanner,
    PreparedFederatedQuery,
    dispatch_federated_nl,
)
from tests._support import Actor
from tests.api.test_invoke_bootstrap import FakeSession, _caller

_DIGEST = "0" * 64


def _prepared(*, sources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "uql": "RETURN n",
        "params": {"name": "arbora"},
        "plan_digest": _DIGEST,
        "query_digest": _DIGEST,
        "sources": sources
        or [
            {
                "source_id": "source:gitlab",
                "limits": {
                    "chunk_bytes": 1024,
                    "chunk_rows": 10,
                    "deadline_ms": 1000,
                    "max_bytes": 4096,
                    "max_rows": 10,
                },
            }
        ],
    }


def _prepared_model(
    *, sources: list[dict[str, Any]] | None = None
) -> PreparedFederatedQuery:
    return PreparedFederatedQuery.model_validate(_prepared(sources=sources))


def _context(services: dict[str, Any]) -> ExecutionContext:
    session = FakeSession(actor=Actor(), tenant="tenant:a")
    caller = _caller(
        session,
        engine_claims=session.engine_verified_context(),
        policy_revision="policy:1",
    )
    return ExecutionContext(
        caller=caller,
        client=object(),
        owner=caller.principal,
        service_identity=False,
        services=services,
    )


@pytest.mark.spec("ARBORA-FLEET-R043.1")
def test_prepared_query_is_closed_and_limits_are_bounded() -> None:
    with pytest.raises(ValidationError):
        PreparedFederatedQuery.model_validate({**_prepared(), "tenant": "other"})
    with pytest.raises(ValidationError):
        PreparedFederatedQuery.model_validate(
            _prepared(
                sources=[
                    {
                        "source_id": "source:gitlab",
                        "limits": {
                            "chunk_bytes": 1024,
                            "chunk_rows": 10,
                            "deadline_ms": 1000,
                            "max_bytes": 4096,
                            "max_rows": 10_001,
                        },
                    }
                ]
            )
        )


@pytest.mark.spec("ARBORA-FLEET-R043.2")
def test_native_plan_adapter_preserves_server_pins_and_empty_uql_params() -> None:
    class Client:
        authenticated = True

        def use_verified_context(self, _: Any) -> Any:
            from contextlib import nullcontext

            return nullcontext()

    context = _context({})
    context = ExecutionContext(
        caller=context.caller,
        client=Client(),
        owner=context.owner,
        service_identity=context.service_identity,
        services=context.services,
        idempotency_key="request:1",
    )
    sent: list[tuple[dict[str, Any], str, str | None]] = []

    async def send_plan(
        _client: Any,
        params: dict[str, Any],
        graph: str,
        *,
        idempotency_key: str | None,
    ) -> object:
        sent.append((params, graph, idempotency_key))
        return object()

    def decode_plan(_: object) -> dict[str, Any]:
        return {
            "uql": "FOREIGN 'source:gitlab' |> LIMIT 1",
            "plan_digest": _DIGEST,
            "query_digest": _DIGEST,
        }

    prepared = asyncio.run(
        NativeNlQueryPlanPlanner(send_plan, decode_plan).prepare_for(
            text="find issues",
            graph="tenant:bound",
            context=context,
        )
    )
    assert sent == [
        ({"text": "find issues", "graph": "tenant:bound"}, "tenant:bound", "request:1")
    ]
    assert prepared.params == {}
    assert prepared.uql == "FOREIGN 'source:gitlab' |> LIMIT 1"
    assert prepared.sources[0].source_id == "source:gitlab"


@pytest.mark.spec("ARBORA-FLEET-R043.1", "ARBORA-FLEET-R043.2")
def test_federated_dispatch_reuses_pinned_uql_and_catalog_provenance() -> None:
    class Planner:
        async def prepare(
            self, *, text: str, graph: str, caller: Any
        ) -> PreparedFederatedQuery:
            del text, graph, caller
            return _prepared_model()

    class Binding:
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
                rows=2,
                pages=1,
                truncated=False,
            )

    sent: list[dict[str, Any]] = []

    async def send_uql(params: Mapping[str, Any]) -> Any:
        sent.append(dict(params))
        return {
            "Rows": {
                "rows": [{"id": "issue:1"}],
                "columns": [],
                "warnings": [],
            },
            "federation": {"native_count": 0, "virtual_count": 1},
        }

    # The production factory is synchronous; keep the fake's shape identical.
    def source_binding(*_: Any, **__: Any) -> Binding:
        return Binding()

    result = asyncio.run(
        dispatch_federated_nl(
            planner=Planner(),
            text="find issues",
            graph="tenant:bound",
            context=_context({"fleet_source_binding": source_binding}),
            send_uql=send_uql,
        )
    )
    assert sent == [
        {
            "text": "RETURN n",
            "params": {"name": "arbora"},
            "foreign_execution": {"execution_id": 7, "query_digest": _DIGEST},
        }
    ]
    assert result["value"]["sources"] == [
        {
            "connector": "gitlab",
            "contract_iri": "iri:contract:gitlab",
            "mapping_iri": "iri:mapping:gitlab",
        }
    ]
    assert result["value"]["rows"] == [{"id": "issue:1"}]


@pytest.mark.spec("ARBORA-FLEET-R043.4")
def test_federated_dispatch_refuses_unattributed_result_counts() -> None:
    class Planner:
        async def prepare(
            self, *, text: str, graph: str, caller: Any
        ) -> PreparedFederatedQuery:
            del text, graph, caller
            return _prepared_model()

    class Binding:
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
                rows=2,
                pages=1,
                truncated=False,
            )

    async def send_uql(_: Mapping[str, Any]) -> Any:
        return {"Rows": {"rows": [{"id": "issue:1"}], "columns": []}}

    with pytest.raises(OperationRefused) as error:
        asyncio.run(
            dispatch_federated_nl(
                planner=Planner(),
                text="find issues",
                graph="tenant:bound",
                context=_context(
                    {"fleet_source_binding": lambda *_args, **_kwargs: Binding()}
                ),
                send_uql=send_uql,
            )
        )
    assert error.value.code == "UNAVAILABLE"
    assert error.value.details == {
        "reason": "federated result lacks authoritative source counts"
    }


@pytest.mark.spec("ARBORA-FLEET-R043.4")
def test_multiple_source_handles_refuse_until_eg_combined_contract_exists() -> None:
    class Planner:
        async def prepare(
            self, *, text: str, graph: str, caller: Any
        ) -> PreparedFederatedQuery:
            del text, graph, caller
            first = _prepared()["sources"][0]
            second = {**first, "source_id": "source:servicenow"}
            return _prepared_model(sources=[first, second])

    async def send_uql(_: Mapping[str, Any]) -> Any:
        return None

    with pytest.raises(OperationRefused) as error:
        asyncio.run(
            dispatch_federated_nl(
                planner=Planner(),
                text="find issues",
                graph="tenant:bound",
                context=_context({}),
                send_uql=send_uql,
            )
        )
    assert error.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R880")
async def test_cross_source_report_served(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated

    from arbora.api.errors import ArboraRefusal
    from arbora.api.ops import query as query_ops
    from tests.api._engine_double import EngineDouble
    from tests.api._engine_double import context as engine_context

    op = next(o for o in query_ops.operations() if o.id == "query.cross_source_report")
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {"CrossSourceReport"}
    )
    with pytest.raises(ArboraRefusal):
        await query_ops.handle_cross_source_report(
            engine_context(), {"request": {}}, op
        )
    engine = EngineDouble(monkeypatch)
    report = {"advisory": "A-1", "reached": ["svc-a"]}
    engine.serve("CrossSourceReport", lambda p: report)
    engine.install(query_ops)
    result = await query_ops.handle_cross_source_report(
        engine_context(), {"request": {"advisory": "A-1"}}, op
    )
    assert result["value"] == report
    assert engine.sent[0][1] == {"request": {"advisory": "A-1"}}


@pytest.mark.spec("ARBORA-FLEET-R043.5")
def test_audit_line_names_sources_and_holds_no_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import hashlib
    import json
    import logging

    class Planner:
        async def prepare(self, *, text: str, graph: str, caller: Any) -> Any:
            return _prepared_model()

    class Binding:
        source = SimpleNamespace(
            connector="gitlab",
            contract=SimpleNamespace(contract_iri="iri:contract:gitlab"),
            mapping=SimpleNamespace(mapping_id="iri:mapping:gitlab"),
        )

        async def read(self, **kwargs: Any) -> Any:
            query_result = await kwargs["correlate"](
                {"execution_id": 7, "query_digest": _DIGEST}
            )
            return SimpleNamespace(
                ok=True, query_result=query_result, rows=1, pages=1, truncated=False
            )

    async def send_uql(params: Mapping[str, Any]) -> Any:
        return {
            "Rows": {
                "rows": [{"id": "SECRET-ROW-VALUE"}],
                "columns": [],
                "warnings": [],
            },
            "federation": {
                "native_count": 0,
                "virtual_count": 1,
                "decision_id": "dec-42",
            },
        }

    with caplog.at_level(logging.INFO, logger="arbora.audit.federation"):
        asyncio.run(
            dispatch_federated_nl(
                planner=Planner(),
                text="find issues",
                graph="tenant:bound",
                context=_context({"fleet_source_binding": lambda *_a, **_k: Binding()}),
                send_uql=send_uql,
            )
        )
    assert "SECRET-ROW-VALUE" not in caplog.text
    line = json.loads(caplog.records[-1].getMessage())
    assert line["question_digest"] == hashlib.sha256(b"find issues").hexdigest()
    assert len(line["question_digest"]) == 64
    assert line["decision_id"] == "dec-42" and line["tenant"] == "tenant:a"
    assert line["sources"][0]["connector"] == "gitlab" and line["principal"]
