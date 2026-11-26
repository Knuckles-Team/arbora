"""The indexed token and cost projection forwards the engine query (ARBORA-OPS-R061)."""

from __future__ import annotations

from typing import Any

import pytest
from tests.api._lane_double import EngineDouble, context

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _engine, usage
from arbora.api.ops.registry_factory import get_registry
from arbora.pin_lag import medulla as pin_lag

OP = next(op for op in usage.operations() if op.id == "usage.index.query")
QUERY = {"window_start_ms": 0, "window_end_ms": 10, "group_by": "model"}
PROFILE = {
    "profile_revision": "1",
    "p95_bound_ms": 500,
    "unit": "ms",
    "boundary": "handler entry to serialized response",
    "workload": "1000000 traces, 32 readers, 1000 queries",
    "indexed_dataset_size": 1_000_000,
    "data_revision": "d1",
    "index_revision": "i1",
    "cache_policy_revision": "c1",
}
AGGREGATE = {
    "groups": [{"key": "m1", "amount": "12.5"}],
    "counters": {"uncached_input": 3, "cached_input": 1},
    "latency_profile": PROFILE,
}


def _engine_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Structural double for a pin that carries the query shape and method."""
    import medulla.generated as generated

    monkeypatch.setattr(pin_lag, "require_models", lambda _names: None)
    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | {"RunTraceUsage"}
    )


@pytest.mark.spec("ARBORA-OPS-R061.1")
def test_models_validate_the_window_and_page_size() -> None:
    parsed = usage.UsageIndexQuery(**QUERY)
    assert parsed.schema_version == 1 and parsed.page_size == 100
    with pytest.raises(ValueError):
        usage.UsageIndexQuery(window_start_ms=5, window_end_ms=1)
    with pytest.raises(ValueError):
        usage.UsageIndexQuery(**QUERY, page_size=1001)
    with pytest.raises(ValueError):  # the caller cannot pick the p95 bound
        usage.UsageIndexQuery(**QUERY, p95_bound_ms=1)


@pytest.mark.spec("ARBORA-OPS-R061.2")
@pytest.mark.asyncio
async def test_projection_is_engine_aggregate_without_local_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    client = EngineDouble(RunTraceUsage=AGGREGATE)
    ctx = context(client, monkeypatch)
    answer = await usage.handle_usage_index_query(ctx, QUERY, OP)
    assert answer["value"] == AGGREGATE  # forwarded unchanged
    method, sent, graph, _ = client.sent[0]
    assert method == "RunTraceUsage" and graph == "tenant-content"
    assert sent["request"] == QUERY and client.claims == [{"principal": "alice"}]


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
    # The pinned engine lacks the query shape: refused, naming it, before any send.
    client = EngineDouble(RunTraceUsage=AGGREGATE)
    with pytest.raises(OperationRefused) as refused:
        await usage.handle_usage_index_query(context(client, monkeypatch), QUERY, OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "RunTraceUsageQuery"
    assert client.sent == []
    _engine_serves(monkeypatch)
    for bad in (
        {**AGGREGATE, "latency_profile": {**PROFILE, "p95_bound_ms": 501}},
        {**AGGREGATE, "latency_profile": {"p95_bound_ms": 100}},
        {k: v for k, v in AGGREGATE.items() if k != "latency_profile"},
    ):
        ctx = context(EngineDouble(RunTraceUsage=bad), monkeypatch)
        with pytest.raises(OperationRefused) as refused:
            await usage.handle_usage_index_query(ctx, QUERY, OP)
        assert refused.value.code == "UNAVAILABLE"
        assert "groups" not in str(refused.value.details)
    # A stale cursor keeps the engine's own code and exposes no rows.
    from medulla import EngineResponseError

    from arbora.api.errors import EngineRefusal

    stale = EngineDouble(RunTraceUsage=EngineResponseError("STALE_CURSOR", "stale"))
    with pytest.raises(EngineRefusal):
        await usage.handle_usage_index_query(
            context(stale, monkeypatch), {**QUERY, "cursor": "old"}, OP
        )


@pytest.mark.spec("ARBORA-OPS-R061", "ARBORA-OPS-R061.5")
@pytest.mark.asyncio
async def test_browser_query_reconciles_with_independent_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    ledger: dict[str, Any] = {"uncached_input": 3, "cached_input": 1}
    ctx = context(EngineDouble(RunTraceUsage=AGGREGATE), monkeypatch)
    answer = await usage.handle_usage_index_query(ctx, QUERY, OP)
    # Each counter equals the independent ledger read; the profile is recorded whole.
    assert answer["value"]["counters"] == ledger
    assert set(answer["value"]["latency_profile"]) == set(usage._PROFILE_FIELDS)
    assert _engine is not None
