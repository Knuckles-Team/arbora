"""The engine-backed gates a pinned release may lack, reported by name (PIN-LAG).

The autonomy, tool-relevance and team-coordination gates compose only once the pinned
engine contract names their ``Decide`` question (:func:`question_kind_served`). A gate
that is not composed is never silent: ``/health/ready`` and ``ops.doctor`` report
``gate_inactive: <name> (engine lacks <QuestionKind>)`` and the host records one audit
event at start. The gates activate by themselves with the re-pin that carries the
questions; delete this module's callers with it.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from arbora.pin_lag.medulla import AUTONOMY_MODELS, missing_model, question_kind_served

__all__ = [
    "GATE_QUESTIONS",
    "GATE_SHAPES",
    "gate_served",
    "gate_inactive_line",
    "inactive_gate_lines",
    "inactive_gates",
]

#: Each conditional gate and the engine ``QuestionKind`` (wire snake_case) it asks.
GATE_QUESTIONS: Mapping[str, str] = MappingProxyType(
    {
        "autonomy": "autonomy_verdict",
        "tool_relevance": "tool_relevance",
        "team_coordination": "team_coordination",
    }
)


#: The typed request and answer shapes a gate also needs, named when they are missing.
GATE_SHAPES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {"autonomy": AUTONOMY_MODELS}
)


def _missing(name: str) -> str | None:
    """What the pinned engine lacks for gate ``name``: its question, else a typed shape."""
    kind = GATE_QUESTIONS[name]
    if not question_kind_served(kind):
        return kind
    return missing_model(GATE_SHAPES.get(name, ()))


def gate_inactive_line(name: str) -> str:
    """The one report line of an inactive gate."""
    return f"gate_inactive: {name} (engine lacks {_missing(name)})"


def gate_served(name: str) -> bool:
    """Whether the pinned engine names the question and shapes of gate ``name``."""
    return _missing(name) is None


def inactive_gates() -> tuple[str, ...]:
    """The gates whose question or typed shapes the pinned contract does not name."""
    return tuple(name for name in GATE_QUESTIONS if not gate_served(name))


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
