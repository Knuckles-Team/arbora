"""Map a sealed process plan onto the harness run plan (ARBORA-OPS-R898.3).

The engine decides the plan: every task is bound to a tool, a prompt, a decision or an
approval, and the plan reaches arbora sealed. Arbora adds no binding. An agent step takes its
prompt text from the prompt the plan binds it to (the task's ``prompt`` is the bound prompt
IRI) and keeps its ``assignee`` as the agent kind. A tool step keeps its call, and the branch,
join, decision and approval steps pass through as the harness step kinds
(``MERISTEM-RUN-R018.1``). A prompt that does not resolve refuses before any step starts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

#: ``prompt IRI -> prompt text``, or ``None`` when the IRI does not resolve.
PromptReader = Callable[[str], Awaitable[str | None]]


class ProcessPromptUnresolved(Exception):
    """An agent step's bound prompt does not resolve (``PROCESS_PROMPT_UNRESOLVED``)."""

    def __init__(self, task_id: str, prompt: str) -> None:
        super().__init__(task_id)
        self.task_id = task_id
        self.prompt = prompt


def _is_agent_step(task: Mapping[str, Any]) -> bool:
    from arbora.api.ops.plans import CONTROL_STEP_KEYS

    return task.get("call") is None and not any(
        isinstance(task.get(key), Mapping) for key in CONTROL_STEP_KEYS
    )


def agent_prompts(plan: Mapping[str, Any]) -> dict[str, str]:
    """``task id -> bound prompt IRI`` of every agent step of the plan."""
    return {
        str(task["task_id"]): str(task["prompt"])
        for task in plan["tasks"]
        if _is_agent_step(task)
    }


async def resolve_prompts(
    read: PromptReader, plan: Mapping[str, Any]
) -> dict[str, str]:
    """``prompt IRI -> text`` for the plan's agent steps, before the run starts anything."""
    texts: dict[str, str] = {}
    for task_id, iri in agent_prompts(plan).items():
        if iri in texts:
            continue
        text = await read(iri)
        if not text:
            raise ProcessPromptUnresolved(task_id, iri)
        texts[iri] = text
    return texts


def map_process_plan(
    plan: Mapping[str, Any],
    prompts: Mapping[str, str],
    *,
    model: Any,
    toolsets: tuple[Any, ...] = (),
) -> Any:
    """The harness ``RunPlan`` of one sealed process plan, agent steps carrying prompt text."""
    from arbora.api.ops.plans import run_plan_for

    bound = dict(plan)
    tasks = []
    for task in plan["tasks"]:
        if _is_agent_step(task):
            iri = str(task["prompt"])
            if iri not in prompts:
                raise ProcessPromptUnresolved(str(task["task_id"]), iri)
            task = {**task, "prompt": prompts[iri]}
        tasks.append(task)
    bound["tasks"] = tasks
    return run_plan_for(bound, model, toolsets)


__all__ = [
    "ProcessPromptUnresolved",
    "PromptReader",
    "agent_prompts",
    "map_process_plan",
    "resolve_prompts",
]
