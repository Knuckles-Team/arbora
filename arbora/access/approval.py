"""Governed decisions for ``ActionApproval`` records.

The decision writes through a session-scoped graph. ``ApprovalDecisions`` holds
the scope it was built with; the host composition builds it with
:func:`verified_approval_session` (ARBORA-HOST-R039.3). Built without a scope,
every decision is refused.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any

__all__ = [
    "ApprovalDecisions",
    "ApprovalSessionScope",
    "verified_approval_session",
]

#: ``scope(engine, required_scope)`` yields the session-scoped graph to write.
ApprovalSessionScope = Callable[[Any, str], AbstractContextManager[Any]]


class _VerifiedApprovalWriter:
    """The engine's CAS, admitted again under the same verified caller."""

    def __init__(self, engine: Any, session: Any, required_scope: str) -> None:
        self._engine = engine
        self._session = session
        self._required_scope = required_scope

    def compare_and_set_node_fields(
        self,
        node_id: str,
        conditions: Mapping[str, Any],
        updates: Mapping[str, Any],
    ) -> bool:
        from arbora.security.session import resolve_session

        resolve_session(self._session, required_scope=self._required_scope)
        return bool(
            self._engine.compare_and_set_node_fields(
                node_id,
                conditions,
                updates,
                idempotency_key=f"{node_id}:decide:{updates.get('status')}",
            )
        )


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
        """Atomically decide one pending ``ActionApproval`` through the verified session."""
        if not str(approval_id).startswith("action_approval:"):
            raise ValueError("approval_id must identify an ActionApproval")
        status = _normalized_status(decision)
        if self._scope is None:
            raise PermissionError("no verified graph session is bound for approvals")
        with self._scope(engine, "kg:write") as graph:
            applied = graph.compare_and_set_node_fields(
                str(approval_id),
                {"status": "pending"},
                {"status": status, "decided_unix": time.time()},
            )
        if not applied:
            raise LookupError("approval is missing or no longer pending")
        return {"approval_id": str(approval_id), "decision": status}
