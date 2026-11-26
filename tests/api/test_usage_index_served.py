"""The run-trace measure forwards the engine ``RunMetrics`` query (ARBORA-OPS-R061)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import usage
from arbora.api.ops.registry_factory import get_registry
from arbora.pin_lag import medulla as pin_lag
from tests.api._lane_double import EngineDouble, context

OP = next(op for op in usage.operations() if op.id == "usage.index.query")
QUERY = {
    "query": "latency",
    "group_by": "technique",
    "window_start_ms": 0,
    "window_end_ms": 10,
    "technique": "rerank",
    "arm": "a",
    "config_digest": "c" * 8,
}
ANSWER = {
    "query": "latency",
    "groups": [{"key": "rerank", "value": 120.0, "trace_count": 4}],
}


def _engine_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural double for a pin that declares the ``RunMetrics`` method."""
    import medulla.generated as generated

    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | {"RunMetrics"}
    )

    async def send(client: Any, method: str, params: Any, graph: str, **keywords: Any):
        return await client._send(method, params, graph, **keywords)

    monkeypatch.setattr(generated, "send_by_id", send)


@pytest.mark.spec("ARBORA-OPS-R061.1")
def test_models_validate_the_query_window_and_filters() -> None:
    parsed = usage.UsageIndexQuery(**QUERY)
    assert parsed.schema_version == 1 and parsed.technique == "rerank"
    for bad in (
        {**QUERY, "window_end_ms": 0},  # empty window
        {k: v for k, v in QUERY.items() if k != "window_end_ms"},  # half a window
        {**QUERY, "query": "tokens_per_second"},  # not a measure
        {**QUERY, "group_by": "model"},  # not a grouping
        {**QUERY, "arm": ""},
        {**QUERY, "p95_bound_ms": 1},  # no field the schema does not name
    ):
        with pytest.raises(ValidationError):
            usage.UsageIndexQuery(**bad)
    assert usage.UsageIndexQuery(query="score").group_by is None


@pytest.mark.spec("ARBORA-OPS-R061.2")
@pytest.mark.asyncio
async def test_projection_is_engine_aggregate_without_local_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    client = EngineDouble(RunMetrics=ANSWER)
    ctx = context(client, monkeypatch)
    answer = await usage.handle_usage_index_query(ctx, QUERY, OP)
    assert answer["value"] == ANSWER  # forwarded unchanged
    method, sent, graph, _ = client.sent[0]
    assert method == "RunMetrics" and graph == "tenant-content"
    assert sent["request"] == {
        "tenant": "tenant-a",  # the verified caller's, never the request's
        "query": "latency",
        "group_by": "technique",
        "window": {"start_ms": 0, "end_ms": 10},
        "technique": "rerank",
        "arm": "a",
        "config_digest": "c" * 8,
    }
    assert client.claims == [{"principal": "alice"}]


@pytest.mark.spec("ARBORA-OPS-R061.3")
def test_registry_lists_operation_and_binder_composes_reader() -> None:
    ids = {op.id for op in get_registry()}
    assert {"usage.index.query", "usage.read"} <= ids
    assert OP.scopes == {"usage:read"} and OP.effect.name == "READ"


@pytest.mark.spec("ARBORA-OPS-R061.4")
@pytest.mark.asyncio
async def test_denied_stale_and_unproven_latency_expose_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla import EngineResponseError

    # The pinned engine lacks the method: refused, naming it, before any send.
    client = EngineDouble(RunMetrics=ANSWER)
    with pytest.raises(OperationRefused) as refused:
        await usage.handle_usage_index_query(context(client, monkeypatch), QUERY, OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "RunMetrics"
    assert client.sent == []
    _engine_serves(monkeypatch)
    # A population that reports no such counter abstains: no group, never a zero.
    abstained = {"query": "latency", "groups": [], "abstained": "NO_MEASUREMENTS"}
    answer = await usage.handle_usage_index_query(
        context(EngineDouble(RunMetrics=abstained), monkeypatch), QUERY, OP
    )
    assert answer["value"] == abstained
    # A malformed answer is unavailable and exposes nothing.
    with pytest.raises(OperationRefused) as malformed:
        await usage.handle_usage_index_query(
            context(EngineDouble(RunMetrics={"query": "latency"}), monkeypatch),
            QUERY,
            OP,
        )
    assert malformed.value.code == "UNAVAILABLE"
    # A foreign tenant is refused by the engine with its own code, and no rows.
    denied = EngineDouble(RunMetrics=EngineResponseError("ACCESS_DENIED", "no"))
    with pytest.raises(EngineRefusal):
        await usage.handle_usage_index_query(context(denied, monkeypatch), QUERY, OP)


@pytest.mark.spec("ARBORA-OPS-R061", "ARBORA-OPS-R061.5")
@pytest.mark.asyncio
async def test_browser_query_reconciles_with_independent_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    ledger = {"cost": 0.5, "tokens": 900.0, "solved": 4.0}
    client = EngineDouble(
        RunMetrics=lambda params: {
            "query": params["request"]["query"],
            "groups": [
                {
                    "key": "all",
                    "value": ledger[params["request"]["query"]],
                    "trace_count": 5,
                }
            ],
        }
    )
    ctx = context(client, monkeypatch)
    for query, expected in ledger.items():
        answer = await usage.handle_usage_index_query(ctx, {"query": query}, OP)
        assert answer["value"]["groups"][0]["value"] == expected
    assert [sent[1]["request"]["query"] for sent in client.sent] == list(ledger)
