"""Workflow tool bindings revalidated before each call (ARBORA-FLEET-R021.1).

A tool declared as a workflow tool needs its admitted declaration and grant
binding before its first call, and the binding is rechecked before every later
call. The grant revision comes from the one grant source the admission gate
used; no second authority exists here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from arbora.api.domain.fleet.admission import WorkflowAdmission

logger = logging.getLogger(__name__)

__all__ = ["REFUSALS", "WorkflowBindings"]

REFUSALS: dict[str, str] = {
    "WORKFLOW_NOT_ADMITTED": "a workflow tool has no admitted binding for this session",
    "WORKFLOW_GRANT_REVOKED": "the grant revision of the binding changed or was revoked",
}


@dataclass(frozen=True)
class _Bound:
    admission: WorkflowAdmission
    grant_source: Callable[[], str | None]


@dataclass
class WorkflowBindings:
    """The workflow tools of one multiplexer and the binding of each session."""

    declared: set[str] = field(default_factory=set)
    _bound: dict[str, _Bound] = field(default_factory=dict)

    def declare(self, tools: frozenset[str] | set[str]) -> None:
        self.declared |= set(tools)

    def bind(
        self,
        session_key: str,
        admission: WorkflowAdmission,
        grant_source: Callable[[], str | None],
    ) -> None:
        self._bound[session_key] = _Bound(admission, grant_source)

    def unbind(self, session_key: str) -> None:
        self._bound.pop(session_key, None)

    def refusal(self, tool: str, session_key: str) -> str | None:
        """The refusal code for ``tool`` in this session, or ``None`` when it may run."""
        if tool not in self.declared:
            return None
        bound = self._bound.get(session_key)
        if bound is None or tool not in bound.admission.tools:
            return "WORKFLOW_NOT_ADMITTED"
        try:
            current = bound.grant_source()
        except Exception:
            logger.warning("refusal failed", exc_info=True)
            return "WORKFLOW_GRANT_REVOKED"
        if current != bound.admission.grant_revision:
            return "WORKFLOW_GRANT_REVOKED"
        return None
