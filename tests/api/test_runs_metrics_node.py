"""Run metrics, node reads and the run trace reader port (ARBORA-OPS-R021.4.1-.4, .6, .9)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import runs
from arbora.api.ops.runs import (
    NODE_CAPABILITIES,
    RunMetricsOutcome,
    RunMetricsParams,
    RunNodeParams,
    RunTraceReader,
    handle_runs_metrics,
    handle_runs_node,
)
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server import run_reader
from arbora.mcp_server.run_reader import NativeRunReader
from arbora.run_origin import RunOriginRegistry
from tests.api.test_run_replay import reader_context


def _op(op_id: str):
    return next(op for op in runs.operations() if op.id == op_id)


class _Reader:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[Any] = []

    async def metrics(self, *, context: Any, request: Any) -> Any:
        self.requests.append(request)
        return self.answer


@pytest.mark.spec("ARBORA-OPS-R021.4.1")
def test_run_trace_reader_is_runtime_checkable_protocol() -> None:
    native = NativeRunReader(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    assert isinstance(native, RunTraceReader)
    assert not isinstance(object(), RunTraceReader)
    assert not isinstance(_Reader({}), RunTraceReader)


@pytest.mark.spec("ARBORA-OPS-R021.4.1")
@pytest.mark.asyncio
async def test_an_absent_reader_is_unavailable_before_any_dispatch() -> None:
    context = SimpleNamespace(services={})
    metrics = await handle_runs_metrics(context, {"query": "cost"}, _op("runs.metrics"))
    node = await handle_runs_node(
        context, {"run_id": "r", "node_id": "n"}, _op("runs.node")
    )
    assert metrics["value"]["availability"] == "unavailable"
    assert node["value"]["availability"] == "unavailable"


@pytest.mark.spec("ARBORA-OPS-R021.4.3")
@pytest.mark.parametrize(
    "values",
    [
        {"query": "cost", "tenant": "other"},
        {"query": ""},
        {"query": "cost", "window": {"start_ms": 9, "end_ms": 1}},
        {"query": "cost", "window": {"start_ms": -1, "end_ms": 1}},
    ],
)
def test_run_metrics_models_reject_unknown_fields(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RunMetricsParams.model_validate(values)


@pytest.mark.spec("ARBORA-OPS-R021.4.3")
@pytest.mark.parametrize(
    "outcome",
    [
        {"query": "cost", "groups": [{"key": "a", "value": -1, "trace_count": 1}]},
        {
            "query": "cost",
            "groups": [{"key": "a", "value": float("inf"), "trace_count": 1}],
        },
        {
            "query": "cost",
            "groups": [{"key": "a", "value": float("nan"), "trace_count": 1}],
        },
        {
            "query": "cost",
            "groups": [{"key": "a", "value": 1, "trace_count": 1}],
            "abstained": "ZERO_SOLVED",
        },
        {"query": "cost", "groups": [], "extra": 1},
    ],
)
def test_run_metric_outcomes_refuse_negative_nonfinite_and_unknown(outcome) -> None:
    with pytest.raises(ValidationError):
        RunMetricsOutcome.model_validate(outcome)


@pytest.mark.spec("ARBORA-OPS-R021.4.4")
@pytest.mark.asyncio
async def test_runs_metrics_passes_engine_values_through() -> None:
    answer = {
        "query": "cost",
        "groups": [
            {"key": "agent-a", "value": 2.5, "trace_count": 4},
            {"key": "agent-b", "value": 0.0, "trace_count": 1},
        ],
        "abstained": None,
    }
    reader = _Reader(answer)
    result = await handle_runs_metrics(
        SimpleNamespace(services={"run_trace_reader": reader}),
        {"query": "cost", "group_by": "agent"},
        _op("runs.metrics"),
    )
    assert [g["key"] for g in result["value"]["groups"]] == ["agent-a", "agent-b"]
    assert result["value"]["groups"][0]["value"] == 2.5
    assert reader.requests[0].group_by == "agent"


@pytest.mark.spec("ARBORA-OPS-R021.4.4")
@pytest.mark.asyncio
async def test_a_zero_solved_abstention_is_not_a_zero_cost() -> None:
    reader = _Reader({"query": "cost", "groups": [], "abstained": "NO_SOLVED_RUNS"})
    result = await handle_runs_metrics(
        SimpleNamespace(services={"run_trace_reader": reader}),
        {"query": "cost"},
        _op("runs.metrics"),
    )
    assert result["value"]["abstained"] == "NO_SOLVED_RUNS"
    assert result["value"]["groups"] == []


@pytest.mark.spec("ARBORA-OPS-R021.4.4")
@pytest.mark.asyncio
async def test_an_invalid_engine_outcome_is_refused_not_repaired() -> None:
    reader = _Reader(
        {"query": "cost", "groups": [{"key": "a", "value": -3, "trace_count": 1}]}
    )
    with pytest.raises(OperationRefused) as caught:
        await handle_runs_metrics(
            SimpleNamespace(services={"run_trace_reader": reader}),
            {"query": "cost"},
            _op("runs.metrics"),
        )
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-OPS-R021.4.4")
def test_metrics_declares_the_telemetry_read_scope() -> None:
    op = _op("runs.metrics")
    assert op.scopes == frozenset({"telemetry:read"})


@pytest.mark.spec("ARBORA-OPS-R021.4.6")
def test_runs_ops_registered_with_existing_scopes() -> None:
    from arbora.api.ops.registry_factory import get_registry

    registry = get_registry()
    for op_id, scope in (
        ("runs.get", "kg:read"),
        ("runs.events", "kg:read"),
        ("runs.execution_graph", "kg:read"),
        ("runs.metrics", "telemetry:read"),
        ("runs.node", "kg:read"),
    ):
        op = registry.get(op_id)
        assert op is not None, op_id
        assert scope in op.scopes


@pytest.mark.spec("ARBORA-OPS-R021.4.2")
@pytest.mark.asyncio
async def test_pinned_engine_refuses_metrics_naming_the_missing_method(
    monkeypatch,
) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) - {"RunMetrics"},
    )
    reader = NativeRunReader(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    with pytest.raises(OperationRefused) as caught:
        await reader.metrics(
            context=reader_context(), request=RunMetricsParams(query="cost")
        )
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.details["missing"] == "RunMetrics"


@pytest.mark.spec("ARBORA-OPS-R021.4.2")
@pytest.mark.asyncio
async def test_metrics_calls_the_engine_with_the_callers_claims(monkeypatch) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | {"RunMetrics"}
    )
    seen: dict[str, Any] = {}

    async def graph(_context: Any) -> str:
        return "tenant-content"

    class Client:
        authenticated = True

        def use_verified_context(self, claims: Any):
            import contextlib

            seen["claims"] = dict(claims)
            return contextlib.nullcontext()

        async def _send(self, method: str, params: Any, graph_name: str) -> Any:
            seen.update(method=method, params=params, graph=graph_name)
            return {"query": "cost", "groups": [], "abstained": "NO_SOLVED_RUNS"}

    monkeypatch.setattr(run_reader, "caller_dispatch_graph", graph)
    context = dataclasses.replace(reader_context(), client=Client())
    reader = NativeRunReader(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    # The structural double stands in for a medulla that serves RunMetrics.
    caller = context.caller
    scoped = dataclasses.replace(
        context,
        caller=dataclasses.replace(
            caller, effective_scopes=caller.effective_scopes | {"telemetry:read"}
        ),
    )
    answer = await reader.metrics(
        context=scoped, request=RunMetricsParams(query="cost", group_by="agent")
    )
    assert seen["method"] == "RunMetrics"
    assert seen["graph"] == "tenant-content"
    assert seen["params"]["tenant"] == scoped.caller.tenant
    assert seen["params"]["group_by"] == "agent"
    assert seen["claims"]["principal"] == scoped.caller.principal
    assert answer["abstained"] == "NO_SOLVED_RUNS"


@pytest.mark.spec("ARBORA-OPS-R021.4.9")
@pytest.mark.asyncio
async def test_node_capabilities_default_unavailable(monkeypatch) -> None:
    reader = NativeRunReader(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    owned = SimpleNamespace(calls=0)

    async def owner(context, run_id, task_id=None):
        owned.calls += 1
        return {"status": "running"}

    monkeypatch.setattr(reader, "_owned", owner)
    value = await reader.node(
        context=reader_context(), request=RunNodeParams(run_id="r", node_id="n")
    )
    assert owned.calls == 1
    assert set(value["capabilities"]) == set(NODE_CAPABILITIES)
    assert all(not c["allowed"] for c in value["capabilities"].values())


@pytest.mark.spec("ARBORA-OPS-R021.4.9")
@pytest.mark.asyncio
async def test_a_foreign_run_node_reads_exactly_like_an_owned_one(monkeypatch) -> None:
    reader = NativeRunReader(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    answers = []
    for item in ({"status": "running"}, None):

        async def owner(context, run_id, task_id=None, item=item):
            return item

        monkeypatch.setattr(reader, "_owned", owner)
        answers.append(
            await reader.node(
                context=reader_context(),
                request=RunNodeParams(run_id="r", node_id="n"),
            )
        )
    assert answers[0] == answers[1]
