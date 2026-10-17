"""Resource priority class of the current request.

An entry point tags its work once with :func:`priority_scope`. Work under that
scope reads the class through :func:`current_priority`: the session stamps it,
and an outbound call carries it in the ``x-resource-priority`` header. An
untagged context is high priority; only background ingestion yields.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from enum import Enum

__all__ = [
    "PRIORITY_HEADER",
    "PriorityClass",
    "bind_priority",
    "current_priority",
    "priority_carrier",
    "priority_scope",
]

PRIORITY_HEADER = "x-resource-priority"


class PriorityClass(Enum):
    """The priority classes. A lower rank is admitted first."""

    INTERACTIVE = "interactive"
    ORCHESTRATION = "orchestration"
    HYDRATION = "hydration"
    BACKGROUND_INGESTION = "background_ingestion"

    @property
    def rank(self) -> int:
        """Numeric precedence; a lower value is a higher priority."""
        return _RANKS[self]

    @property
    def is_background(self) -> bool:
        """True only for the one class that yields to all others."""
        return self is PriorityClass.BACKGROUND_INGESTION


_RANKS: dict[PriorityClass, int] = {
    PriorityClass.INTERACTIVE: 0,
    PriorityClass.ORCHESTRATION: 1,
    PriorityClass.HYDRATION: 1,
    PriorityClass.BACKGROUND_INGESTION: 3,
}

_priority: contextvars.ContextVar[PriorityClass | None] = contextvars.ContextVar(
    "arbora_priority_class", default=None
)


def current_priority() -> PriorityClass | None:
    """The priority class of the current task, or ``None`` when untagged."""
    return _priority.get()


@contextlib.contextmanager
def priority_scope(priority: PriorityClass | None) -> Iterator[None]:
    """Bind ``priority`` for the duration of a ``with`` block."""
    token = _priority.set(priority)
    try:
        yield
    finally:
        _priority.reset(token)


def bind_priority(value: PriorityClass | str | None) -> None:
    """Restore a priority from a carrier value; an unknown value clears it."""
    if value is None or isinstance(value, PriorityClass):
        _priority.set(value)
        return
    try:
        _priority.set(PriorityClass(str(value)))
    except ValueError:
        _priority.set(None)


def priority_carrier() -> dict[str, str]:
    """The active priority as a header carrier for an outbound call."""
    current = _priority.get()
    return {PRIORITY_HEADER: current.value} if current is not None else {}
