"""Executing one admitted plan: the harness run, its tool binding and the reply steps (ARBORA-HOST-R038.11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.screening import (
    ScreenSettings,
    redact_result,
    screen_settings,
)
from arbora.api.ops.plan_fleet_steps import (
    _REFUSED_STEP_CODES,
    _RUN_TOOLS,
    FOREACH_SOURCE_ARGUMENT,
    FleetStepTools,
    _fleet_calls,
    _plan_calls,
    _step_toolsets,
    governed_tool_caller,
)


def _task_call(task: Mapping[str, Any]) -> Any:
    """The harness ``ToolStep`` of a typed tool task, or ``None`` for a prompt step.

    The engine's canonical call maps one to one. A foreach step shows the
    harness one step that binds only the source array; the per-item template
    stays in :class:`FleetStepTools`, because the harness resolves ``$ref``
    values itself and an ``item.`` reference is not one of its bindings.
    """
    from meristem.graph import ToolStep

    call = task.get("call")
    if not isinstance(call, Mapping):
        return None
    foreach = task.get("foreach")
    if isinstance(foreach, Mapping):
        arguments: Any = {FOREACH_SOURCE_ARGUMENT: dict(foreach["source"])}
    else:
        arguments = call.get("arguments") or {}
    return ToolStep(server=call["server"], tool=call["tool"], arguments=arguments)


#: The control step kinds of an engine plan task (MERISTEM-RUN-R018.1).
CONTROL_STEP_KEYS: tuple[str, ...] = ("branch", "join", "decision", "approval")


def _control_steps(task: Mapping[str, Any]) -> dict[str, Any]:
    """The task's control step as ``RunTask`` fields; a pinned harness without them refuses."""
    found = {
        k: dict(task[k]) for k in CONTROL_STEP_KEYS if isinstance(task.get(k), Mapping)
    }
    if found:
        from arbora.pin_lag.meristem import PinLagUnavailable, require_task_fields

        try:
            require_task_fields(tuple(found))
        except PinLagUnavailable as lag:
            from arbora.api.ops.pin_guards import refusal

            raise refusal(lag) from lag
    return found


def run_plan_for(plan: Mapping[str, Any], model: Any, toolsets: tuple = ()) -> Any:
    """The harness ``RunPlan`` for an engine plan: same tasks, edges and limits."""
    from meristem.graph import (
        DependencyPlan,
        RunBudget,
        RunPlan,
        RunTask,
        StopRule,
    )

    graph = DependencyPlan(
        tasks=tuple(
            RunTask(
                task_id=task["task_id"],
                prompt=task["prompt"],
                depends_on=tuple(task["depends_on"]),
                call=_task_call(task),
                **_control_steps(task),
            )
            for task in plan["tasks"]
        ),
        output_task_id=plan["output_task_id"],
        max_concurrency=int(plan["max_concurrency"]),
        plan_id=plan["plan_digest"],
    )
    return RunPlan(
        model=model,
        dependency_plan=graph,
        toolsets=tuple(toolsets),
        stop=StopRule(max_steps=int(plan["max_steps"])),
        budget=RunBudget(max_concurrent_requests=int(plan["max_concurrency"])),
    )


def _screen(context: Any) -> ScreenSettings:
    """The screen settings of the host config the operation context carries."""
    return screen_settings(context.services["config"])


def _redacted(value: Any, screen: ScreenSettings) -> Any:
    return redact_result(value, screen)[0]


def _reply_steps(
    plan: Mapping[str, Any],
    tasks: Mapping[str, Mapping[str, Any]],
    fleet: FleetStepTools | None,
    screen: ScreenSettings,
) -> list[dict[str, Any]]:
    """Each tool step in declaration order, from the harness record and the ledger.

    Arbora computes none of the values (ARBORA-RELEASE-R003.4.8, R003.4.16):
    arguments, outcome and result come from the harness record, the key and
    the audit reference from the pipeline call. Secrets are masked.
    """
    ledger = fleet.ledger if fleet is not None else []
    return [
        _reply_step(
            task, (tasks.get(task["task_id"]) or {}).get("tool_step"), ledger, screen
        )
        for task in _plan_calls(plan)
    ]


def _step_evidence(
    task: Mapping[str, Any], entries: list[Any], screen: ScreenSettings
) -> dict[str, Any]:
    """The pipeline half of a step reply: per-item entries, or the one call's key."""
    if isinstance(task.get("foreach"), Mapping):
        return {"items": [_reply_item(entry, screen) for entry in entries]}
    first = entries[0] if entries else {}
    return {
        "idempotency_key": first.get("idempotency_key"),
        "audit_ref": first.get("audit_ref"),
    }


def _reply_step(
    task: Mapping[str, Any],
    record: Mapping[str, Any] | None,
    ledger: list[Any],
    screen: ScreenSettings,
) -> dict[str, Any]:
    record = record or {}
    entries = [e for e in ledger if e["task_id"] == task["task_id"]]
    reason = record.get("error") or (None if record else "not_started")
    step: dict[str, Any] = {
        "task_id": task["task_id"],
        "resolved_arguments": _redacted(record.get("arguments"), screen),
        "outcome": record.get("outcome") or "refused",
        "failure_reason": _redacted(reason, screen),
        "result": _redacted(record.get("result"), screen),
        **_step_evidence(task, entries, screen),
    }
    if record.get("reference"):
        step["failed_reference"] = record["reference"]
    return step


def _reply_item(entry: Mapping[str, Any], screen: ScreenSettings) -> dict[str, Any]:
    code = entry.get("error")
    outcome = (
        "ok"
        if entry.get("ok")
        else "refused"
        if code in _REFUSED_STEP_CODES
        else "failed"
    )
    return {
        "index": entry.get("item_index"),
        "resolved_arguments": _redacted(entry.get("arguments"), screen),
        "outcome": outcome,
        "failure_reason": code,
        "result": _redacted(entry.get("result"), screen),
        "idempotency_key": entry.get("idempotency_key"),
        "audit_ref": entry.get("audit_ref"),
    }


def _bind_run_tools(plan: Mapping[str, Any], fleet: FleetStepTools | None) -> Any:
    """Bind the run's governed tools; returns the context token to reset."""
    if _plan_calls(plan) and fleet is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "no governed tool caller is bound"}
        )
    return _RUN_TOOLS.set(fleet)


def _host_ports(context: Any) -> Any:
    """The host's ``HostPorts`` for a run this operation starts, or ``None`` unbound."""
    slot = context.services.get("run_ports")
    return None if slot is None else slot.current()


def _run_request(plan: Mapping[str, Any], run_id: str, ports: Any) -> Any:
    """The run request; it carries the ``HostPorts`` the run executes over.

    A plan with typed tool steps adds the governed caller to the record. The caller
    reads the run's own tools from ``_RUN_TOOLS``, so the record holds no run state.
    """
    from meristem.graph import RunRequest

    extra = {} if ports is None else {"ports": ports}
    return RunRequest(task=plan["output_task_id"], run_id=run_id, **extra)


def _ports_with_caller(plan: Mapping[str, Any], ports: Any) -> Any:
    """``ports`` with the governed caller added when the plan has typed tool steps."""
    from dataclasses import replace

    from meristem.host_ports import HostPorts

    if not _plan_calls(plan):
        return ports
    return replace(
        ports if ports is not None else HostPorts(), caller=governed_tool_caller
    )


def _harness_plan(
    plan: Mapping[str, Any],
    model: Any,
    fleet: FleetStepTools | None,
    prompts: Mapping[str, str] | None,
) -> Any:
    """The run plan: a process plan resolves its prompt texts, any other maps one to one."""
    if prompts is None:
        return run_plan_for(plan, model, _step_toolsets(fleet))
    from arbora.api.domain.processes.run_plan import map_process_plan

    return map_process_plan(plan, prompts, model=model, toolsets=_step_toolsets(fleet))


async def _drain_run(
    plan: Mapping[str, Any],
    model: Any,
    run_id: str,
    fleet: FleetStepTools | None,
    *,
    ports: Any = None,
    prompts: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any]]:
    """Run the harness and collect each task's terminal event.

    A process run (ARBORA-OPS-R898.5) passes the ``HostPorts`` that bind its decision port
    and approval waiter, and the resolved prompt texts its agent steps take.
    """
    from meristem.graph import iterate_run

    request = _run_request(plan, run_id, _ports_with_caller(plan, ports))
    tasks: dict[str, dict[str, Any]] = {}
    started: list[str] = []
    terminal: dict[str, Any] = {}
    run_plan = _harness_plan(plan, model, fleet, prompts)
    async for event in iterate_run(request, {}, plan=run_plan):
        kind, task_id = event.get("type"), event.get("task_id")
        if kind == "task_started" and task_id:
            started.append(task_id)
        elif str(kind).startswith("task_") and task_id:
            tasks[task_id] = {
                key: event.get(key)
                for key in ("status", "output", "error", "tool_step")
            }
        elif kind in ("graph_complete", "error"):
            terminal = dict(event.get("result") or {})
    return tasks, started, terminal


async def execute_task_plan(
    plan: Mapping[str, Any],
    model: Any,
    *,
    run_id: str,
    fleet: FleetStepTools | None = None,
    screen: ScreenSettings,
    ports: Any = None,
    prompts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run the plan in the harness and collect every task's terminal result."""
    token = _bind_run_tools(plan, fleet)
    try:
        tasks, started, terminal = await _drain_run(
            plan, model, run_id, fleet, ports=ports, prompts=prompts
        )
    finally:
        _RUN_TOOLS.reset(token)
    return {
        "status": terminal.get("status"),
        "error": terminal.get("error"),
        "output": (tasks.get(plan["output_task_id"]) or {}).get("output"),
        "started_order": started,
        "tasks": tasks,
        "fleet_calls": _fleet_calls(fleet),
        "steps": _reply_steps(plan, tasks, fleet, screen),
    }
