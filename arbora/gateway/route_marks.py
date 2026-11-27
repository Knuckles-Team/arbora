"""Mark a handler with its route, and build a router from the marked handlers.

A gateway router is built per host by ``build_<name>_router()``; it is never a
module-level object. A handler carries its route as an attribute of its own, and the
builder mounts every marked function of its module in definition order
(ARBORA-HOST-R039.7).
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from starlette.routing import Route

__all__ = ["build_router", "plain_routes", "route"]

_ATTRIBUTE = "__gateway_route__"


def plain_routes(router: APIRouter) -> list[Route]:
    """The router's plain HTTP routes, in order; websocket mounts are not bridged."""
    return [entry for entry in router.routes if isinstance(entry, Route)]


def route(method: str, path: str, **options: Any) -> Callable[[Any], Any]:
    """Mark a handler as ``method`` ``path``; ``options`` go to ``add_api_route``."""

    def mark(handler: Any) -> Any:
        setattr(handler, _ATTRIBUTE, (method, path, options))
        return handler

    return mark


def build_router(module_name: str, **router_options: Any) -> APIRouter:
    """A new router holding every handler of ``module_name`` marked with :func:`route`."""
    router = APIRouter(**router_options)
    for handler in tuple(vars(sys.modules[module_name]).values()):
        mark = (
            getattr(handler, _ATTRIBUTE, None) if inspect.isfunction(handler) else None
        )
        if mark is not None:
            method, path, options = mark
            router.add_api_route(path, handler, methods=[method], **options)
    return router
