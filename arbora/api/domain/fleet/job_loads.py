"""Per-job tool loading from the engine's answer (ARBORA-FLEET-R037.1, R037.2).

Arbora computes no ranking. The engine answers a ``LoadSet`` question with the
tools, skills and local agents the job needs, in order. Arbora loads the
answered tools in answer order until the session cap, lists the rest as
``over_cap`` and never loads what the answer omits or reorders by a score.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    kept = tools[:session_cap]
    return JobLoadResult(
        loaded=tuple(str(t["id"]) for t in kept),
        over_cap=tuple(str(t["id"]) for t in tools[session_cap:]),
        skills=_names(answer, "skills"),
        agent_starts=_names(answer, "agents"),
        decision_id=str(answer.get("decision_id", "")),
        premises={str(t["id"]): str(t["premise"]) for t in kept if t.get("premise")},
    )


def _names(answer: Mapping[str, Any], key: str) -> tuple[str, ...]:
    return tuple(str(name) for name in answer.get(key) or ())


#: The engine's refusal when a job asks for more tools than the session may hold.
PARAMETER_INVALID = "PARAMETER_INVALID"
DECISION_UNAVAILABLE = "decision_unavailable"


def _answer_of(response: Mapping[str, Any]) -> Mapping[str, Any]:
    """The ``LoadSet`` answer inside one ``Decide`` response."""
    for key in ("answer", "value"):
        inner = response.get(key)
        if isinstance(inner, Mapping):
            return inner
    return response


def _normalized(answer: Mapping[str, Any]) -> dict[str, Any]:
    """The engine's tool names with their ``load_set:component=`` premises."""
    premises: dict[str, str] = {}
    for ref in answer.get("premises") or ():
        text = ref if isinstance(ref, str) else (ref or {}).get("id", "")
        if isinstance(text, str) and text.startswith("load_set:component="):
            premises[text.removeprefix("load_set:component=")] = text
    tools = []
    for tool in answer.get("tools") or ():
        if isinstance(tool, Mapping):
            tools.append(dict(tool))
        else:
            tools.append({"id": str(tool), "premise": premises.get(str(tool))})
    return {**answer, "tools": tools}


async def find_for_job(
    gateway: Any,
    caller: Any,
    decide: Callable[[dict[str, Any]], Awaitable[Mapping[str, Any]]],
    request: JobLoadRequest,
    *,
    max_agents: int = 0,
) -> dict[str, Any]:
    """Ask the engine which tools the job needs and load exactly those.

    ARBORA-FLEET-R037.3/.4/.5: ``decide`` sends one ``Decide`` question as the
    verified caller. An abstention loads nothing and returns its reason; an
    unreachable engine returns ``decision_unavailable``; every other refusal,
    ``PARAMETER_INVALID`` included, keeps the engine's code. There is no
    fallback ranking or keyword search.
    """
    question = request.question(gateway.sessions.cap, max_agents)
    try:
        response = await decide(question)
    except (ConnectionError, TimeoutError, OSError):
        return {"loaded": [], "refusal": DECISION_UNAVAILABLE}
    abstain = response.get("abstain") or response.get("abstain_reason")
    if abstain:
        return {"loaded": [], "abstain_reason": abstain}
    answer = _normalized(_answer_of(response))
    answer.setdefault("decision_id", response.get("decision_id", ""))
    result = await gateway.load_job_answer(caller, answer)
    return {
        "loaded": list(result.loaded),
        "over_cap": list(result.over_cap),
        "skills": list(result.skills),
        "agent_starts": list(result.agent_starts),
        "decision_id": result.decision_id,
        "premises": dict(result.premises or {}),
    }
