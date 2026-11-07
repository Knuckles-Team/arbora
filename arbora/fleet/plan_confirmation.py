"""Answer a connector's destructive-action confirmation with a confirmed plan.

ARBORA-FLEET-R015.2 (gap G15). A connector built on agent-connector-sdk asks
for a live MCP confirmation (``elicitation/create``) before a destructive tool
runs, and fails closed without one. Arbora is the MCP client of its children,
so it answers that request itself, from authority it already holds:

* The invoke pipeline only dispatches a destructive ``fleet.call`` under a
  consumed, human-confirmed plan (``Confirm.PLAN``); an expired or mismatched
  ``plan_ref`` is refused before dispatch.
* :func:`confirmed_plan_call` marks that one dispatch: the tool the plan
  confirmed, in the caller's context.
* :meth:`PlanConfirmations.armed` holds the mark on the exact child session
  for exactly the duration of that tool call, one armed call per session at a
  time.
* :meth:`PlanConfirmations.answer_elicitation` accepts a confirmation only on an armed session,
  only once per armed call, only for the boolean ``confirmed`` form the SDK
  asks, and only when the request names the armed tool. Everything else is
  declined, so an unconfirmed call keeps failing closed.

There is no bypass flag and no direct source call: the connector still runs
its own guard and receives an ordinary MCP confirmation answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import weakref
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from mcp import types

__all__ = ["PlanConfirmations", "confirmed_plan_call", "plan_confirmed"]

#: The child tool a consumed plan confirmed for the current dispatch.
_CONFIRMED_TOOL: ContextVar[str | None] = ContextVar(
    "arbora_confirmed_plan_tool", default=None
)
_ACTION = re.compile(r"^Are you sure you want to (?P<action>[A-Za-z0-9_.:-]{1,256})\?$")


@dataclass
class _Armed:
    tool: str
    used: bool = False


@contextlib.contextmanager
def confirmed_plan_call(tool: str) -> Iterator[None]:
    """Mark this dispatch as the destructive call a consumed plan confirmed."""
    token = _CONFIRMED_TOOL.set(tool)
    try:
        yield
    finally:
        _CONFIRMED_TOOL.reset(token)


def plan_confirmed(tool: str) -> bool:
    """Whether this dispatch is the call a consumed plan confirmed for ``tool``."""
    return _CONFIRMED_TOOL.get() == tool


def _asks_boolean_confirmation(params: Any) -> bool:
    schema = getattr(params, "requested_schema", None)
    if not isinstance(schema, dict):
        return False
    confirmed = (schema.get("properties") or {}).get("confirmed")
    return isinstance(confirmed, dict) and confirmed.get("type") == "boolean"


def _names_tool(message: Any, tool: str) -> bool:
    found = _ACTION.match(message) if isinstance(message, str) else None
    if found is None:
        return False
    action = found.group("action")
    return action == tool or tool.endswith("_" + action)


class PlanConfirmations:
    """The armed child sessions of one fleet; one armed call per session at a time."""

    __slots__ = ("_armed", "_locks")

    def __init__(self) -> None:
        self._armed: weakref.WeakKeyDictionary[Any, _Armed] = (
            weakref.WeakKeyDictionary()
        )
        self._locks: weakref.WeakKeyDictionary[Any, asyncio.Lock] = (
            weakref.WeakKeyDictionary()
        )

    @contextlib.asynccontextmanager
    async def armed(self, session: Any, tool: str) -> AsyncIterator[None]:
        """Arm ``session`` for one confirmation while the confirmed ``tool`` runs."""
        if _CONFIRMED_TOOL.get() != tool:
            yield
            return
        lock = self._locks.setdefault(session, asyncio.Lock())
        async with lock:
            self._armed[session] = _Armed(tool)
            try:
                yield
            finally:
                self._armed.pop(session, None)

    async def answer_elicitation(self, context: Any, params: Any) -> types.ElicitResult:
        """The child session's elicitation callback: confirm an armed call, else decline."""
        current = self._armed.get(getattr(context, "session", None))
        if (
            current is None
            or current.used
            or not _asks_boolean_confirmation(params)
            or not _names_tool(getattr(params, "message", None), current.tool)
        ):
            return types.ElicitResult(action="decline")
        current.used = True
        return types.ElicitResult(action="accept", content={"confirmed": True})
