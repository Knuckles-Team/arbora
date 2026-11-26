"""The proposal outcome projection forwards the engine join (ARBORA-OPS-R059)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import evolution_metrics as metrics
from arbora.api.ops.registry_factory import get_registry
from arbora.pin_lag import medulla as pin_lag
from tests.api._lane_double import EngineDouble, context

OP = metrics.operations()[0]
QUERY = {
    "proposal_id": "p1",
    "proposal_revision": 2,
    "prompt_version_id": "v1",
    "prompt_revision": 1,
    "prompt_digest": "a" * 64,
    "metric_window_id": "w1",
    "metric_window_revision": 3,
    "metric_window_digest": "b" * 64,
}
ANSWER = {
    "trace_refs": ["t1", "t2"],
    "missing_ids": ["t3"],
    "state": "partial",
    "metrics": [{"metric_id": "m", "state": "unknown", "value": None}],
}


def _engine_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(pin_lag, "require_models", lambda _names: None)
    monkeypatch.setattr(metrics, "require_models", lambda _names: None)
    monkeypatch.setattr(metrics, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {metrics.METHOD},
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)


@pytest.mark.spec("ARBORA-OPS-R059.1")
def test_models_round_trip_rust_vectors() -> None:
    parsed = metrics.EvolutionMetricsQuery(**QUERY)
    assert parsed.model_dump() == {**QUERY, "schema_version": 1}
    for bad in ({**QUERY, "prompt_digest": "xyz"}, {**QUERY, "proposal_id": ""}):
        with pytest.raises(ValidationError):
            metrics.EvolutionMetricsQuery(**bad)
    with pytest.raises(ValidationError):  # every identity is required
        metrics.EvolutionMetricsQuery(
            **{k: v for k, v in QUERY.items() if k != "proposal_id"}
        )


@pytest.mark.spec("ARBORA-OPS-R059.2")
@pytest.mark.asyncio
async def test_projection_forwards_caller_scope_and_keeps_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    client = EngineDouble(EvolutionTraceMetrics=ANSWER)
    answer = await metrics.handle_evolution_metrics(
        context(client, monkeypatch), QUERY, OP
    )
    assert answer["value"] == ANSWER  # partial stays partial, never zero
    method, sent, graph, _ = client.sent[0]
    assert (method, graph) == ("EvolutionTraceMetrics", "tenant-content")
    assert sent == {"request": QUERY} and client.claims == [{"principal": "alice"}]
    assert OP.scopes == {"loops:read"} and OP.effect.name == "READ"
    assert "evolution.proposals.metrics" in {op.id for op in get_registry()}


@pytest.mark.spec("ARBORA-OPS-R059.3")
@pytest.mark.asyncio
async def test_denied_or_incomplete_requests_read_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla import EngineResponseError

    # The pinned engine lacks the method: UNAVAILABLE naming it, zero engine calls.
    client = EngineDouble(EvolutionTraceMetrics=ANSWER)
    with pytest.raises(OperationRefused) as refused:
        await metrics.handle_evolution_metrics(context(client, monkeypatch), QUERY, OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == metrics.MODEL and client.sent == []
    # An incomplete identity never reaches the handler: the model refuses it.
    with pytest.raises(ValidationError):
        metrics.EvolutionMetricsQuery(**{**QUERY, "metric_window_digest": ""})
    # Engine refusals keep their code and disclose no progress or metrics.
    _engine_serves(monkeypatch)
    for code in ("SUBJECT_ACCESS_DENIED", "STALE_REVISION"):
        engine = EngineDouble(EvolutionTraceMetrics=EngineResponseError(code, "no"))
        with pytest.raises(EngineRefusal) as engine_refused:
            await metrics.handle_evolution_metrics(
                context(engine, monkeypatch), QUERY, OP
            )
        assert engine_refused.value.code == code


@pytest.mark.spec("ARBORA-OPS-R059", "ARBORA-OPS-R059.4")
@pytest.mark.asyncio
async def test_dashboard_joins_proposal_to_trace_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    complete = {
        "trace_refs": ["t1", "t2"],
        "missing_ids": [],
        "state": "complete",
        "metrics": [],
    }
    client = EngineDouble(EvolutionTraceMetrics=complete)
    answer = await metrics.handle_evolution_metrics(
        context(client, monkeypatch), QUERY, OP
    )
    assert answer["value"]["trace_refs"] == ["t1", "t2"]  # the engine's order
    assert answer["value"]["state"] == "complete"
