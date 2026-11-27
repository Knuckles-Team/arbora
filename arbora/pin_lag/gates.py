"""The engine-backed gates a pinned release may lack, reported by name (PIN-LAG).

The autonomy, tool-relevance and team-coordination gates compose only once the pinned
engine contract names their ``Decide`` question (:func:`question_kind_served`). A gate
that is not composed is never silent: ``/health/ready`` and ``ops.doctor`` report
``gate_inactive: <name> (engine lacks <QuestionKind>)`` and the host records one audit
event at start. The gates activate by themselves with the re-pin that carries the
questions; delete this module's callers with it.
"""

from __future__ import annotations

from arbora.pin_lag.medulla import question_kind_served

__all__ = [
    "GATE_QUESTIONS",
    "gate_inactive_line",
    "inactive_gate_lines",
    "inactive_gates",
]

#: Each conditional gate and the engine ``QuestionKind`` (wire snake_case) it asks.
GATE_QUESTIONS: dict[str, str] = {
    "autonomy": "autonomy_verdict",
    "tool_relevance": "tool_relevance",
    "team_coordination": "team_coordination",
}


def gate_inactive_line(name: str) -> str:
    """The one report line of an inactive gate."""
    return f"gate_inactive: {name} (engine lacks {GATE_QUESTIONS[name]})"


def inactive_gates() -> tuple[str, ...]:
    """The gates whose question the pinned contract does not name."""
    return tuple(
        name for name, kind in GATE_QUESTIONS.items() if not question_kind_served(kind)
    )


def inactive_gate_lines() -> list[str]:
    """One ``gate_inactive: ...`` line per inactive gate, in gate order."""
    return [gate_inactive_line(name) for name in inactive_gates()]


def gate_checks() -> dict[str, str]:
    """The ``ops.doctor`` checks of the inactive gates, keyed ``gate.<name>``."""
    return {f"gate.{name}": gate_inactive_line(name) for name in inactive_gates()}


def ready_report() -> dict[str, list[str]]:
    """The ``/health/ready`` body part of the inactive gates; empty when all are active."""
    lines = inactive_gate_lines()
    return {"gate_inactive": lines} if lines else {}
