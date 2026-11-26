"""Per-job tool loading from the engine's answer (ARBORA-FLEET-R037.1, R037.2).

Arbora computes no ranking. The engine answers a ``LoadSet`` question with the
tools, skills and local agents the job needs, in order. Arbora loads the
answered tools in answer order until the session cap, lists the rest as
``over_cap`` and never loads what the answer omits or reorders by a score.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["JobLoadRequest", "JobLoadResult", "apply_load_set"]


@dataclass(frozen=True)
class JobLoadRequest:
    job_text: str
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.job_text.strip():
            raise ValueError("a job load request carries the job text")
        object.__setattr__(
            self, "required_capabilities", tuple(self.required_capabilities)
        )

    def question(self, max_tools: int, max_agents: int) -> dict[str, Any]:
        """The engine ``LoadSetQuestion``; ``max_tools`` is the session cap."""
        return {
            "job_text": self.job_text,
            "required_capabilities": list(self.required_capabilities),
            "max_tools": max_tools,
            "max_agents": max_agents,
        }


@dataclass(frozen=True)
class JobLoadResult:
    loaded: tuple[str, ...] = ()
    over_cap: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    agent_starts: tuple[str, ...] = ()
    decision_id: str = ""
    #: tool id -> the engine ``load_set:component=`` premise that selected it
    premises: Mapping[str, str] | None = None


def apply_load_set(answer: Mapping[str, Any], session_cap: int) -> JobLoadResult:
    """Apply one engine answer: its order, its cap, nothing of arbora's own."""
    if session_cap < 0:
        raise ValueError("the session cap is not negative")
    tools: Sequence[Mapping[str, Any]] = answer.get("tools") or ()
    loaded = tuple(str(t["id"]) for t in tools[:session_cap])
    over = tuple(str(t["id"]) for t in tools[session_cap:])
    premises = {
        str(t["id"]): str(t["premise"]) for t in tools[:session_cap] if t.get("premise")
    }
    return JobLoadResult(
        loaded=loaded,
        over_cap=over,
        skills=tuple(str(s) for s in answer.get("skills") or ()),
        agent_starts=tuple(str(a) for a in answer.get("agents") or ()),
        decision_id=str(answer.get("decision_id", "")),
        premises=premises,
    )
