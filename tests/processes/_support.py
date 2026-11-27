"""Structural doubles for process runs: the sealed plan, the main-shape harness, the engine.

The pinned meristem predates the control step kinds and ``HostPorts`` (``MERISTEM-RUN-R018``);
these doubles declare the main shapes so the real arbora mapping and ports run against them.
The run loop double executes the mapped plan in order and applies each control step through
the bound ports, exactly as the harness does for its received plan.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import pytest

from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401
from tests.processes.test_approvals import _Journal

__all__ = [
    "PLAN",
    "PRINCIPAL",
    "ProcessEngine",
    "approver_clock",
    "main_run_task",
    "run_loop",
]

PRINCIPAL = "principal:one"
SCHEMA = {"component_id": "decide.schema.process"}

PLAN: dict[str, Any] = {
    "plan_digest": "sha256:plan",
    "output_task_id": "x1",
    "max_concurrency": 2,
    "max_steps": 10,
    "tasks": [
        {
            "task_id": "t1",
            "prompt": "",
            "depends_on": [],
            "call": {"server": "srv", "tool": "fetch", "arguments": {}},
        },
        {
            "task_id": "a1",
            "prompt": "prompt:triage",
            "depends_on": ["t1"],
            "assignee": "agent:triage",
        },
        {
            "task_id": "ap",
            "prompt": "",
            "depends_on": ["a1"],
            "approval": {"approver_role": "role:finance", "timeout_s": 30},
        },
        {
            "task_id": "b1",
            "prompt": "",
            "depends_on": ["ap"],
            "branch": {
                "kind": "exclusive",
                "arms": [
                    {"target_task_id": "x1"},
                    {"target_task_id": "x2"},
                ],
            },
        },
        {"task_id": "x1", "prompt": "prompt:ship", "depends_on": ["b1"]},
        {"task_id": "x2", "prompt": "prompt:hold", "depends_on": ["b1"]},
    ],
}

PROMPTS = {
    "prompt:triage": "Triage the fetched records.",
    "prompt:ship": "Ship the approved change.",
    "prompt:hold": "Hold the change for review.",
}


@pytest.fixture
def main_run_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """``RunTask`` with the four control step fields of meristem ``main``."""
    import meristem.graph as graph

    base = graph.RunTask

    @dataclasses.dataclass(frozen=True)
    class RunTask(base):  # type: ignore[valid-type, misc]
        branch: Mapping[str, Any] | None = None
        join: Mapping[str, Any] | None = None
        decision: Mapping[str, Any] | None = None
        approval: Mapping[str, Any] | None = None

    monkeypatch.setattr(graph, "RunTask", RunTask)


def advisory(*options: str) -> dict[str, Any]:
    """The engine's advisory outcome of a control step: scored option ids."""
    return {
        "outcome": "advisory",
        "scores": [{"option_id": o, "score": {"value": 1 << 32}} for o in options],
    }


def decide_record(
    request: Mapping[str, Any], value: Mapping[str, Any]
) -> dict[str, Any]:
    """The engine's sealed record answering ``request`` for the test principal."""
    digest = hashlib.sha256(PRINCIPAL.encode()).hexdigest()
    return {
        "caller_principal": f"principal:sha256:{digest}",
        "tenant_id": request["tenant_id"],
        "question": request["question"],
        "inputs": {"params": request["params"]},
        "record_id": f"rec:{request['question']['kind']}",
        "record_digest": "d1",
        "answer": {"type": request["question"]["kind"], "value": dict(value)},
        "premises": [{"id": "binding:a1"}],
    }


class ProcessEngine:
    """The engine methods a process run reaches: ``Decide``, node reads and the journal."""

    def __init__(self, *, arm: str = "x1", tenant: str = "tenant:one") -> None:
        self.journal = _Journal()
        self.arm, self.tenant, self.sent = arm, tenant, []

    async def send(
        self, _context: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        self.sent.append((method, params))
        if method == "GetNodeProperties":
            text = PROMPTS.get(params["node_id"])
            return {"properties": {"content": text}} if text else {}
        if method == "Decide":
            return self._decide(params["request"])
        return await self.journal(method, params, None)

    def _decide(self, request: Mapping[str, Any]) -> dict[str, Any]:
        kind = request["question"]["kind"]
        if request["tenant_id"] != self.tenant:
            from arbora.api.errors import EngineRefusal

            raise EngineRefusal("WORKFLOW_UNKNOWN")
        if kind == "process_plan":
            return {"records": [decide_record(request, PLAN)]}
        record = decide_record(request, {})
        record["outcome"] = advisory(f"taken:{self.arm}", "default:false")
        return {"records": [record]}


class _Loop:
    """One run of the mapped plan: each control step goes through the bound ports."""

    def __init__(
        self, plan: Mapping[str, Any], run: Any, run_id: str, ports: Any
    ) -> None:
        self.plan, self.tasks, self.run_id, self.ports = (
            plan,
            run.dependency_plan.tasks,
            run_id,
            ports,
        )
        self.done: dict[str, Any] = {}
        self.skipped: set[str] = set()
        self.status = "completed"

    async def approve(self, task: Any) -> None:
        spec = task.approval
        outcome = await self.ports.approval(
            self.run_id, task.task_id, spec["approver_role"], 30, "plan"
        )
        self.done[task.task_id] = {"verdict": outcome.verdict}
        if outcome.verdict != "approve":
            self.status = "failed"
            self.skipped.update(t.task_id for t in self.tasks if t is not task)

    async def branch(self, task: Any) -> None:
        payload = {
            "plan_digest": self.plan["plan_digest"],
            "branch_task_id": task.task_id,
        }
        verdict = await self.ports.decision("BranchSelect", payload, task.task_id)
        arms = {a["target_task_id"] for a in task.branch["arms"]}
        self.skipped.update(arms - set(verdict.taken_task_ids))
        self.done[task.task_id] = {"taken": list(verdict.taken_task_ids)}

    async def step(self, task: Any) -> None:
        if task.approval:
            await self.approve(task)
        elif task.branch:
            await self.branch(task)
        elif task.call is None:
            self.done[task.task_id] = {"prompt": task.prompt}

    async def run(self) -> dict[str, Any]:
        for task in self.tasks:
            if task.task_id in self.skipped or set(task.depends_on) & self.skipped:
                self.skipped.add(task.task_id)
                continue
            await self.step(task)
        out = self.done.get(self.plan["output_task_id"])
        return {
            "status": self.status,
            "error": None,
            "output": json.dumps(out),
            "started_order": list(self.done),
            "tasks": self.done,
        }


async def run_loop(
    plan: Mapping[str, Any],
    model: Any,
    *,
    run_id: str,
    fleet: Any = None,
    ports: Any = None,
    prompts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The harness double: apply the mapped plan's steps in order through the bound ports."""
    from arbora.processes.run_plan import map_process_plan

    run = map_process_plan(plan, prompts or {}, model=model)
    return await _Loop(plan, run, run_id, ports).run()


def approver_clock(broker: Any, seen: dict[str, Any], approved: list[str]) -> Any:
    """A controlled clock: the poll tick is the finance approver deciding; the timeout never fires."""
    import asyncio

    async def wait(seconds: float) -> None:
        if seconds >= 30:
            await asyncio.Event().wait()
        for row in await broker.records("tenant:one", seen["run_id"]):
            if row.get("state") != "pending":
                continue
            await broker.resolve(
                "tenant:one",
                run_id=row["run_id"],
                step_id=row["step_id"],
                verdict="approve",
                comment="",
                principal="person:finance",
                groups=("role:finance",),
                decided_ms=7,
            )
            approved.append(row["step_id"])

    return wait
