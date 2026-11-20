"""Governed decisions for ``action.approval`` control leases.

The decision runs under a verified session scope. ``ApprovalDecisions`` holds
the scope it was built with; the host composition builds it with
:func:`verified_approval_session` (ARBORA-HOST-R039.3). Built without a scope,
every decision is refused. The decision itself is the engine's
``TransitionControlLease`` on the approval lease (ARBORA-IDENTITY-R019.9).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from arbora.access.approval_leases import APPROVAL_ID_PREFIX, decide_approval

__all__ = [
    "ApprovalDecisions",
    "ApprovalSessionScope",
    "verified_approval_session",
]

#: ``scope(engine, required_scope)`` yields the writer that decides one lease.
ApprovalSessionScope = Callable[[Any, str], AbstractContextManager[Any]]


class _VerifiedApprovalWriter:
    """The engine's lease transition, admitted again under the same caller."""

    def __init__(self, engine: Any, session: Any, required_scope: str) -> None:
        self._engine = engine
        self._session = session
        self._required_scope = required_scope

    def decide(self, approval_id: str, status: str) -> None:
        from arbora.security.session import resolve_session

        resolve_session(self._session, required_scope=self._required_scope)
        decide_approval(self._engine, approval_id, status)


@contextmanager
def verified_approval_session(engine: Any, required_scope: str) -> Iterator[Any]:
    """Yield a writer bound to the verified ambient session; refuse without one."""
    from arbora.security.session import resolve_session

    session = resolve_session(required_scope=required_scope)
    yield _VerifiedApprovalWriter(engine, session, required_scope)


def _normalized_status(decision: str) -> str:
    normalized = str(decision).strip().lower()
    if normalized in {"approve", "approved"}:
        return "approved"
    if normalized in {"deny", "denied", "reject", "rejected"}:
        return "denied"
    raise ValueError("decision must be approved or denied")


class ApprovalDecisions:
    """Approval decisions of one host, through the scope it was built with."""

    __slots__ = ("_scope",)

    def __init__(self, scope: ApprovalSessionScope | None = None) -> None:
        self._scope = scope

    def decide(self, engine: Any, approval_id: str, decision: str) -> dict[str, str]:
        """Decide one pending approval lease through the verified session.

        Raises:
            ValueError: not an approval id, or not an approve/deny decision.
            PermissionError: no verified session scope is bound.
            LookupError: the approval is missing or no longer pending.
        """
        if not str(approval_id).startswith(APPROVAL_ID_PREFIX):
            raise ValueError("approval_id must identify an action approval")
        status = _normalized_status(decision)
        if self._scope is None:
            raise PermissionError("no verified graph session is bound for approvals")
        with self._scope(engine, "kg:write") as writer:
            writer.decide(str(approval_id), status)
        return {"approval_id": str(approval_id), "decision": status}
