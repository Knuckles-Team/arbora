"""Doubles for the A2A harness-admission tests."""

from __future__ import annotations

from typing import Any

from arbora.a2a.admission import (
    HarnessConfigMirror,
    HarnessConfigRecord,
    ToolSubsetAdmissionRequest,
)
from arbora.api.domain.a2a.models import A2AContextBudget

DIGEST = "ab" * 32
TOOLS = ["kg.query", "kg.write"]


def part(value: Any = None, **extra: Any) -> dict[str, Any]:
    if extra:
        return {"decision": "abstained", **extra}
    return {"decision": "decided", "value": value}


def answer_json(**overrides: Any) -> dict[str, Any]:
    """The engine's ``HarnessConfigAnswer`` JSON, every part decided."""
    parts: dict[str, Any] = {
        "action_space": part("tool_set"),
        "context_policy": part("elide"),
        "hard_threshold": part({"numerator": 9, "denominator": 10}),
        "plan_scaffold": part("off"),
        "recent_window_fraction": part({"numerator": 1, "denominator": 4}),
        "soft_threshold": part({"numerator": 3, "denominator": 4}),
        "tool_subset": part(list(TOOLS)),
    }
    parts.update(overrides)
    return parts


def record(**overrides: Any) -> HarnessConfigRecord:
    fields = {k: v for k, v in overrides.items() if k in {"expires_at_ms"}}
    parts = {k: v for k, v in overrides.items() if k not in fields}
    return HarnessConfigRecord(
        answer=HarnessConfigMirror.model_validate(answer_json(**parts)),
        decision_record_ref="record:harness:1",
        digest=DIGEST,
        **fields,
    )


def request(tokens: int | None = 4_096) -> ToolSubsetAdmissionRequest:
    return ToolSubsetAdmissionRequest(
        agent_graph_ref="graph:one",
        context_budget=None if tokens is None else A2AContextBudget(tokens=tokens),
        model_profile_ref="profile:one",
        task_class="task:chat",
        tenant="tenant:one",
        priority="interactive",
    )


class Scripted:
    """A decider double: one scripted answer (or failure), every call recorded."""

    def __init__(self, answer: HarnessConfigRecord | Exception | None = None) -> None:
        self.answer = answer if answer is not None else record()
        self.calls: list[dict[str, Any]] = []

    async def decide(self, **fields: Any) -> HarnessConfigRecord:
        self.calls.append(fields)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer
