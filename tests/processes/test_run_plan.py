"""A sealed process plan maps onto the harness run plan (ARBORA-OPS-R898.3)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.domain.processes.run_plan import (
    ProcessPromptUnresolved,
    agent_prompts,
    map_process_plan,
    resolve_prompts,
)
from tests.processes._support import PLAN, PROMPTS, main_run_task  # noqa: F401


@pytest.mark.spec("ARBORA-OPS-R898.3")
def test_agent_steps_take_bound_prompt_text(main_run_task: None) -> None:  # noqa: F811
    run = map_process_plan(PLAN, PROMPTS, model="model:one")
    tasks = {t.task_id: t for t in run.dependency_plan.tasks}
    # An agent step takes the text its bound prompt IRI resolves to.
    assert tasks["a1"].prompt == PROMPTS["prompt:triage"]
    assert tasks["x1"].prompt == PROMPTS["prompt:ship"]
    # A tool step keeps its call and its (empty) prompt.
    assert tasks["t1"].call is not None and tasks["t1"].call.tool == "fetch"
    # Branch and approval steps pass through as harness step kinds, unchanged.
    assert tasks["ap"].approval == {"approver_role": "role:finance", "timeout_s": 30}
    assert tasks["b1"].branch["kind"] == "exclusive"
    assert run.dependency_plan.plan_id == PLAN["plan_digest"]
    assert tasks["b1"].depends_on == ("ap",)
    assert set(agent_prompts(PLAN)) == {"a1", "x1", "x2"}


@pytest.mark.spec("ARBORA-OPS-R898.3")
async def test_an_unresolved_prompt_refuses_before_any_step(
    main_run_task: None,  # noqa: F811
) -> None:
    asked: list[str] = []

    async def read(iri: str) -> str | None:
        asked.append(iri)
        return None if iri == "prompt:ship" else PROMPTS[iri]

    with pytest.raises(ProcessPromptUnresolved) as refused:
        await resolve_prompts(read, PLAN)
    assert refused.value.task_id == "x1" and refused.value.prompt == "prompt:ship"
    with pytest.raises(ProcessPromptUnresolved):
        map_process_plan(PLAN, {"prompt:triage": "t"}, model="m")


@pytest.mark.spec("ARBORA-OPS-R898.3")
def test_a_pinned_harness_without_the_step_kinds_refuses_naming_them() -> None:
    import dataclasses

    from meristem.graph import RunTask

    if "approval" in {f.name for f in dataclasses.fields(RunTask)}:
        pytest.skip("the pin carries the control step kinds")
    from arbora.api.invoke.pipeline import OperationRefused

    with pytest.raises(OperationRefused) as refused:
        map_process_plan(PLAN, PROMPTS, model="m")
    details: Any = refused.value.details
    assert refused.value.code == "UNAVAILABLE"
    assert str(details["missing"]).startswith("meristem.graph.RunTask.")
