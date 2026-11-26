"""Transition history of a task, read through to the engine ledger (ARBORA-A2A-R011).

Every WorkItem state change is appended to the engine's transition ledger and the engine
serves it in pages. This module reads one page after the ownership check of the task and
returns the engine entries unchanged: it reconstructs nothing locally and keeps no copy.

``ListWorkItemTransitions`` is an engine ``main`` shape the pinned release lacks (PIN-LAG:
remove the guard after the re-pin that carries ``work_items.list_transitions``): until then
a history request refuses with ``A2ATransitionHistoryUnavailable``, never an empty or
partial history.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from .authority import A2ATransitionHistoryUnavailable, WorkItemA2AAuthority
from .models import TRANSITION_PAGE_LIMIT, A2ATransition, A2ATransitionPage

__all__ = ["EngineTransitionHistory"]

_METHOD = "list_transitions"


class EngineTransitionHistory:
    """``A2ATransitionHistoryAuthority`` over the engine WorkItem transition ledger."""

    def __init__(self, authority: WorkItemA2AAuthority) -> None:
        self._authority = authority

    def serves(self) -> bool:
        """Whether the engine in use serves the ledger read (the pin carries it)."""
        try:
            _engine, work_engine = self._authority._engine()
        except RuntimeError:
            return False
        return (
            getattr(getattr(work_engine, "work_items", None), _METHOD, None) is not None
        )

    async def transition_history(
        self,
        task_id: str,
        *,
        cursor: str | None = None,
        limit: int = TRANSITION_PAGE_LIMIT,
    ) -> A2ATransitionPage | None:
        """One ledger page of a task the caller owns; ``None`` for a foreign or absent one."""
        if not 1 <= limit <= TRANSITION_PAGE_LIMIT:
            raise ValueError("limit is out of range")
        authority = self._authority
        item = await asyncio.to_thread(authority._authorized_item, task_id, "kg:read")
        if item is None:
            return None
        _engine, work_engine = authority._engine()
        namespace = getattr(work_engine, "work_items", None)
        read = getattr(namespace, _METHOD, None)
        if read is None:
            raise A2ATransitionHistoryUnavailable(
                "durable task transition history is not available"
            )
        session = authority._session("kg:read")
        work_item_id = authority._work_item_id(task_id)
        answered: Any = read(
            tenant=str(session.tenant),
            work_item_id=work_item_id,
            cursor=cursor,
            limit=limit,
        )
        if inspect.isawaitable(answered):
            answered = await answered
        if answered is None:
            # The ownership check just saw this item: an absent ledger is not an answer.
            raise A2ATransitionHistoryUnavailable(
                "durable task transition history is not available"
            )
        return A2ATransitionPage(
            entries=[
                A2ATransition(
                    state=str(entry["state"]),
                    at_ms=entry["at_ms"],
                    revision=entry["revision"],
                )
                for entry in answered["entries"]
            ],
            next_cursor=answered.get("next_cursor"),
        )
