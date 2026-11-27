"""The request a running scale reconcile serves, bound for the actuator thread.

The reconcile runs the synchronous actuator on a worker thread; the actuator reaches the
fleet through the verified request and the loop that started it. The binding is one
context key, so the served operation binds it and the fleet actuator reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused

__all__ = ["bind_request", "bound_request", "request_context"]

#: The one request-bound dispatcher the sync actuator reads: ``(context, loop)``.
_REQUEST: contextvars.ContextVar[tuple[Any, asyncio.AbstractEventLoop] | None] = (
    contextvars.ContextVar("fleet_actuation_request", default=None)
)


@contextlib.contextmanager
def bind_request(context: Any) -> Iterator[None]:
    """Bind the running request to the actuator for the span of one reconcile."""
    token = _REQUEST.set((context, asyncio.get_running_loop()))
    try:
        yield
    finally:
        _REQUEST.reset(token)


def request_context() -> Any:
    """The verified request the running reconcile serves; refuses when none is bound."""
    return bound_request()[0]


def bound_request() -> tuple[Any, asyncio.AbstractEventLoop]:
    """The bound ``(context, loop)``; refuses when no request is bound."""
    bound = _REQUEST.get()
    if bound is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "no request is bound"})
    return bound
