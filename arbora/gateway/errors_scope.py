"""Bind the host's error store for each request the gateway serves (ARBORA-HOST-R039.4)."""

from __future__ import annotations

from typing import Any

from arbora.observability.state import Errors
from arbora.security.error_surface import bind_errors, unbind_errors

__all__ = ["ErrorsScopeMiddleware"]


class ErrorsScopeMiddleware:
    """ASGI middleware: every request runs with the host's :class:`Errors` bound."""

    def __init__(self, app: Any, errors: Errors) -> None:
        self.app = app
        self._errors = errors

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        token = bind_errors(self._errors)
        try:
            await self.app(scope, receive, send)
        finally:
            unbind_errors(token)
