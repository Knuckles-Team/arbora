"""Nominal zero-effect local browser port for optional host composition."""

from collections.abc import Awaitable, Callable
from typing import Any

LOCAL_BROWSER_PATH = "/ws/local-browser-admission"
_Handler = Callable[[Any, Any, Any], Awaitable[None]]


class LocalBrowserBoundary:
    """Opaque in-process port; request data cannot construct owner authority."""

    __slots__ = ("_handler",)
    _handler: _Handler

    def __init__(self) -> None:
        raise TypeError("Local browser boundary must be issued by its owner")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self._handler(scope, receive, send)


def _issue_local_browser_boundary(handler: _Handler) -> LocalBrowserBoundary:
    boundary = object.__new__(LocalBrowserBoundary)
    boundary._handler = handler
    return boundary
