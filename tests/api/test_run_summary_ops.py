"""Per-run projection preparation; native served acceptance remains pending."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import runs
from tests.api._ops_support import tenant_reader_context

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.5")


class Reader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    async def get(self, **kwargs):
        self.calls.append(kwargs)
        return self.value


async def invoke(op_by_id, reader, params=None):
    context = tenant_reader_context(reader=reader, service_name="run_trace_reader")
    op = op_by_id(runs.operations(), "runs.get")
    assert op.scopes == frozenset({"kg:read"})
    result = await runs.handle_runs_get(context, params or {"run_id": "run-one"}, op)
    return context, result


async def test_preserves_zero_and_missing_measurements(op_by_id):
    reader = Reader(
        runs.RunSummary(run_id="run-one", cost_usd=0, peak_context_tokens=0)
    )
    context, result = await invoke(op_by_id, reader)
    assert reader.calls == [{"run_id": "run-one", "context": context}]
    assert result["availability"] == "available"
    assert result["value"]["cost_usd"] == 0
    assert result["value"]["peak_context_tokens"] == 0
    assert result["value"]["context_budget_tokens"] is None
    assert result["value"]["harness_config"] is None
    assert result["value"]["current_plan"] == {
        "availability": "unavailable",
        "snapshot": None,
    }


async def test_absent_reader_refuses(op_by_id):
    with pytest.raises(OperationRefused, match="UNAVAILABLE"):
        await invoke(op_by_id, None)


async def test_unknown_run_is_unavailable(op_by_id):
    _, result = await invoke(op_by_id, Reader(None))
    assert result == {"availability": "unavailable", "value": None}


@pytest.mark.parametrize(
    "value",
    [
        {"run_id": "run-one"},
        runs.RunSummary(run_id="other"),
        runs.RunSummary.model_construct(run_id="run-one", cost_usd=float("nan")),
    ],
)
async def test_invalid_producer_refuses(op_by_id, value):
    with pytest.raises(OperationRefused, match="UNAVAILABLE"):
        await invoke(op_by_id, Reader(value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("peak_context_tokens", -1),
        ("peak_context_tokens", True),
        ("context_budget_tokens", "100"),
        ("cost_usd", -1),
        ("cost_usd", float("inf")),
        ("cost_usd", float("nan")),
        ("termination_reason", "success"),
        ("invented", 1),
    ],
)
def test_summary_refuses_invalid_measurements(field, value):
    with pytest.raises(ValidationError):
        runs.RunSummary.model_validate({"run_id": "run-one", field: value})


async def test_authority_cannot_be_supplied_as_input(op_by_id):
    reader = Reader(None)
    with pytest.raises(ValidationError):
        await invoke(op_by_id, reader, {"run_id": "run-one", "tenant": "other"})
    assert reader.calls == []


async def test_generated_client_schema_accepts_projection(op_by_id):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError as SchemaValidationError

    from arbora.client._generated_models import schema_for

    _, result = await invoke(op_by_id, Reader(runs.RunSummary(run_id="run-one")))
    schema = schema_for("runs.get")
    Draft202012Validator(schema["result"]["schema"]).validate(result)
    with pytest.raises(SchemaValidationError):
        Draft202012Validator(schema["params"]["schema"]).validate(
            {"run_id": "run-one", "tenant": "other"}
        )
