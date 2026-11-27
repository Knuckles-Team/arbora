"""The scheduled paper order door (ARBORA-DATA-MARKET-R006).

A due DCA occurrence in paper mode fills through the one existing paper order handler,
``arbora.api.ops.finance.handle_orders_paper_submit``; there is no second order path. The
owner already confirmed the plan and delegated the grant, so the worker runs the handler
as the host service caller under the operation's own executor scopes. Before the handler
it runs the same ownership check the pipeline runs for a service executor
(``prepare_executor``): a delegated fill is admitted by the engine's grant ledger and an
undelegated one is refused ``SUBJECT_ACCESS_DENIED``.

A plan is a notional amount and the paper order takes a quantity. The door reads the
venue quote at fill time, derives the quantity from the notional in exact decimal and
answers the price reference with the fill; a notional is never passed as a quantity. The
venue call inside still goes through the served ``fleet.call`` pipeline. The invoke
services exist only after composition, so the door binds late and refuses ``UNAVAILABLE``
before then.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from arbora.api.domain.finance.dca import (
    DCAEvaluationError,
    price_reference,
    quantity_for_notional,
)
from arbora.api.invoke.executor import execution_context, prepare_executor
from arbora.api.invoke.pipeline import OperationRefused

__all__ = ["LatePaperDoor"]

PAPER_OP = "orders.paper_submit"
QUOTE_TOOL = "emerald_market_data"
QUOTE_SOURCE = "emerald-exchange:emerald_market_data.quote"


def _price(quote: Mapping[str, Any]) -> Decimal:
    try:
        return Decimal(str(quote["last"]))
    except (KeyError, InvalidOperation):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the venue quote has no usable price"}
        ) from None


def _with_reference(answer: Any, reference: dict[str, str] | None) -> Any:
    return answer if reference is None else {**answer, "price_reference": reference}


class LatePaperDoor:
    """``PaperDoor`` over the served invoke services, bound once they exist."""

    def __init__(
        self,
        caller: Callable[[], Any],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._caller = caller
        self._clock = clock
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
        params = dict(arguments)
        caller = self._caller()
        await self._admit(op, params, caller)
        async with execution_context(
            op, caller, services.runtime, key, None, services.registry.digest
        ) as context:
            context = replace(context, operation_services=services)
            reference = await self._price_notional(context, params)
            answer = await handle_orders_paper_submit(context, params, op)
        return _with_reference(answer, reference)

    async def _admit(self, op: Any, params: dict[str, Any], caller: Any) -> None:
        """The pipeline's own ownership check for a service executor, before any call."""
        refused = await prepare_executor(op, params, caller, self._services.runtime)
        if refused is not None:
            raise OperationRefused(refused.code, dict(refused.details))

    async def _price_notional(
        self, context: Any, params: dict[str, Any]
    ) -> dict[str, str] | None:
        """Replace a ``notional`` by the quantity it buys at the quote read now."""
        from arbora.api.ops.finance import venue_call

        if "notional" not in params:
            return None
        notional = Decimal(str(params.pop("notional")))
        quote = await venue_call(
            context, QUOTE_TOOL, {"action": "quote", "symbol": params["symbol"]}
        )
        price = _price(quote)
        try:
            quantity = quantity_for_notional(notional, price)
        except DCAEvaluationError as error:
            raise OperationRefused(
                "INVALID_ARGUMENT", {"reason": str(error)}
            ) from error
        params["qty"] = float(quantity)
        return price_reference(
            QUOTE_SOURCE, params["symbol"], price, self._clock().isoformat()
        )
