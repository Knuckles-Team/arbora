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
* :func:`armed` holds the mark on the exact child session for exactly the
  duration of that tool call, one armed call per session at a time.
* :func:`answer_elicitation` accepts a confirmation only on an armed session,
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

__all__ = ["answer_elicitation", "armed", "confirmed_plan_call"]

#: The child tool a consumed plan confirmed for the current dispatch.
_CONFIRMED_TOOL: ContextVar[str | None] = ContextVar(
    "arbora_confirmed_plan_tool", default=None
)
_ACTION = re.compile(r"^Are you sure you want to (?P<action>[A-Za-z0-9_.:-]{1,256})\?$")


@dataclass
class _Armed:
    tool: str
    used: bool = False


_ARMED: weakref.WeakKeyDictionary[Any, _Armed] = weakref.WeakKeyDictionary()
_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


@contextlib.contextmanager
def confirmed_plan_call(tool: str) -> Iterator[None]:
    """Mark this dispatch as the destructive call a consumed plan confirmed."""
    token = _CONFIRMED_TOOL.set(tool)
    try:
        yield
    finally:
        _CONFIRMED_TOOL.reset(token)


@contextlib.asynccontextmanager
async def armed(session: Any, tool: str) -> AsyncIterator[None]:
    """Arm ``session`` for one confirmation while the confirmed ``tool`` runs."""
    if _CONFIRMED_TOOL.get() != tool:
        yield
        return
    lock = _LOCKS.setdefault(session, asyncio.Lock())
    async with lock:
        _ARMED[session] = _Armed(tool)
        try:
            yield
        finally:
            _ARMED.pop(session, None)


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


async def answer_elicitation(context: Any, params: Any) -> types.ElicitResult:
    """The child session's elicitation callback: confirm an armed call, else decline."""
    current = _ARMED.get(getattr(context, "session", None))
    if (
        current is None
        or current.used
        or not _asks_boolean_confirmation(params)
        or not _names_tool(getattr(params, "message", None), current.tool)
    ):
        return types.ElicitResult(action="decline")
    current.used = True
    return types.ElicitResult(action="accept", content={"confirmed": True})
