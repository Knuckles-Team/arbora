"""The scheduled paper order door (ARBORA-DATA-MARKET-R006).

A due DCA occurrence in paper mode fills through the one existing paper order handler,
``arbora.api.ops.finance.handle_orders_paper_submit``; there is no second order path. The
owner already confirmed the plan and delegated the grant, so the worker runs the handler
as the host service caller under the operation's own executor scopes. The venue call
inside still goes through the served ``fleet.call`` pipeline. The invoke services exist
only after composition, so the door binds late and refuses ``UNAVAILABLE`` before then.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from arbora.api.invoke.executor import execution_context
from arbora.api.invoke.pipeline import OperationRefused

__all__ = ["LatePaperDoor"]

PAPER_OP = "orders.paper_submit"


class LatePaperDoor:
    """``PaperDoor`` over the served invoke services, bound once they exist."""

    def __init__(self, caller: Callable[[], Any]) -> None:
        self._caller = caller
        self._services: Any = None

    def bind(self, services: Any) -> None:
        if self._services is not None:
            raise RuntimeError("the paper order door is already bound")
        self._services = services

    async def __call__(self, plan: Any, arguments: Mapping[str, Any], key: str) -> Any:
        from arbora.api.ops.finance import handle_orders_paper_submit

        services = self._services
        op = services.registry.get(PAPER_OP) if services is not None else None
        if op is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the paper order door is not bound"}
            )
        async with execution_context(
            op, self._caller(), services.runtime, key, None, services.registry.digest
        ) as context:
            context = replace(context, operation_services=services)
            return await handle_orders_paper_submit(context, dict(arguments), op)
