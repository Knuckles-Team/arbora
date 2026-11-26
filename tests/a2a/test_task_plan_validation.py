"""A model task plan that breaks the plan model is refused with a typed code (ARBORA-A2A-R026)."""

from __future__ import annotations

import json

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.mcp_server.task_plan_runtime import PLANNER_INSTRUCTIONS, _decode_proposal


def _plan(**overrides: object) -> str:
    plan: dict[str, object] = {
        "tasks": [{"task_id": "a", "prompt": "do", "depends_on": []}],
        "output_task_id": "a",
    }
    return json.dumps({**plan, **overrides})


@pytest.mark.spec("ARBORA-A2A-R026")
def test_integer_task_ids_are_refused_typed_with_field_paths() -> None:
    integer_ids = _plan(
        tasks=[
            {"task_id": 1, "prompt": "secret-prompt", "depends_on": [2]},
            {"task_id": "b", "prompt": "ok", "depends_on": []},
        ],
        output_task_id=1,
    )
    with pytest.raises(OperationRefused) as refused:
        _decode_proposal(integer_ids)
    assert refused.value.code == "TASK_PLAN_INVALID"
    assert refused.value.details == {
        "fields": [
            "output_task_id",
            "tasks.0.depends_on.0",
            "tasks.0.task_id",
        ]
    }
    # Paths only: no value the model returned reaches the refusal.
    assert "secret-prompt" not in repr(refused.value.details)


@pytest.mark.spec("ARBORA-A2A-R026")
def test_a_valid_plan_decodes_and_nothing_coerces_an_id() -> None:
    assert _decode_proposal(_plan())["output_task_id"] == "a"
    with pytest.raises(OperationRefused) as refused:
        _decode_proposal(_plan(output_task_id=7))
    assert refused.value.code == "TASK_PLAN_INVALID"


@pytest.mark.spec("ARBORA-A2A-R026")
def test_the_planner_request_carries_the_plan_schema_with_string_ids() -> None:
    schema_text = PLANNER_INSTRUCTIONS.split("JSON schema: ", 1)[1]
    schema = json.loads(schema_text)
    assert schema["properties"]["output_task_id"]["type"] == "string"
    task = schema["$defs"]["ProposedTask"]["properties"]
    assert task["task_id"]["type"] == "string"
    assert task["depends_on"]["items"]["type"] == "string"
