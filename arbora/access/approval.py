"""Governed decisions for ``ActionApproval`` records.

The decision writes through a session-scoped graph. The composition root binds
:func:`bind_approval_session`; with no binding, a decision is refused.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

#: ``scope(engine, required_scope)`` yields the session-scoped graph to write.
ApprovalSessionScope = Callable[[Any, str], AbstractContextManager[Any]]

_session_scope: ApprovalSessionScope | None = None


def bind_approval_session(scope: ApprovalSessionScope | None) -> None:
    """Bind (or with ``None`` clear) the session scope for approval decisions."""
    global _session_scope
    _session_scope = scope


def _normalized_status(decision: str) -> str:
    normalized = str(decision).strip().lower()
    if normalized in {"approve", "approved"}:
        return "approved"
    if normalized in {"deny", "denied", "reject", "rejected"}:
        return "denied"
    raise ValueError("decision must be approved or denied")


def decide_action_approval(
    engine: Any, approval_id: str, decision: str
) -> dict[str, str]:
    """Atomically decide one pending ``ActionApproval`` through the verified session."""
    if not str(approval_id).startswith("action_approval:"):
        raise ValueError("approval_id must identify an ActionApproval")
    status = _normalized_status(decision)
    scope = _session_scope
    if scope is None:
        raise PermissionError("no verified graph session is bound for approvals")
    with scope(engine, "kg:write") as graph:
        applied = graph.compare_and_set_node_fields(
            str(approval_id),
            {"status": "pending"},
            {"status": status, "decided_unix": time.time()},
        )
    if not applied:
        raise LookupError("approval is missing or no longer pending")
    return {"approval_id": str(approval_id), "decision": status}
