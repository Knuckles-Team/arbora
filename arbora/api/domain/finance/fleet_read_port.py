"""The marketplace read port and its fleet adapter (ARBORA-DATA-MARKET-R003.6).

``FinanceReadPort`` is the one upstream contract of the nine marketplace reads. A
request carries the tenant and account Arbora already resolved from verified
authority, never a caller parameter. ``FleetFinanceReadPort`` implements it over
the governed fleet call of the venue connector, so the reads reach the venue only
through the admitted tool surface; no provider client is imported here. The port
has no order method: a read cannot reach a submission or reserve an effect.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

#: The venue connector server and the read tools it publishes through the fleet.
VENUE_SERVER = "emerald-exchange"
BINDING_TOOL = "emerald_exchange_account_binding"
TOOLS: Mapping[str, str] = {
    "list_markets": "emerald_exchange_list_markets",
    "search_markets": "emerald_exchange_search_markets",
    "get_market": "emerald_exchange_get_market",
    "list_events": "emerald_exchange_list_events",
    "search_events": "emerald_exchange_search_events",
    "get_event": "emerald_exchange_get_event",
    "list_orders": "emerald_exchange_list_orders",
    "search_orders": "emerald_exchange_search_orders",
    "get_order": "emerald_exchange_get_order",
}


class ReadRequest(BaseModel):
    """One typed read, already bound to the verified tenant and account."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: str
    account: str
    limit: int | None = None
    position: str | None = None
    snapshot: str | None = None
    filters: Mapping[str, Any] = {}
    query: str | None = None
    resource_id: str | None = None


Answer = Mapping[str, Any]


class FinanceReadPort(Protocol):
    """The nine typed reads and the one account binding authority."""

    async def account_binding(self, context: Any, tenant: str) -> str | None:
        """The account verified for ``tenant``, or ``None`` when unverified."""

    async def list_markets(self, context: Any, request: ReadRequest) -> Answer: ...

    async def search_markets(self, context: Any, request: ReadRequest) -> Answer: ...

    async def get_market(self, context: Any, request: ReadRequest) -> Answer: ...

    async def list_events(self, context: Any, request: ReadRequest) -> Answer: ...

    async def search_events(self, context: Any, request: ReadRequest) -> Answer: ...

    async def get_event(self, context: Any, request: ReadRequest) -> Answer: ...

    async def list_orders(self, context: Any, request: ReadRequest) -> Answer: ...

    async def search_orders(self, context: Any, request: ReadRequest) -> Answer: ...

    async def get_order(self, context: Any, request: ReadRequest) -> Answer: ...


def _arguments(request: ReadRequest) -> dict[str, Any]:
    """The tool arguments of one read; an unset field stays unset."""
    # The tenant is the caller's own, carried by the governed fleet call; a tenant
    # field in the arguments would be a caller-asserted authority, so none is sent.
    body: dict[str, Any] = {"account": request.account}
    for key in ("limit", "position", "snapshot", "query", "resource_id"):
        value = getattr(request, key)
        if value is not None:
            body[key] = value
    if request.filters:
        body["filters"] = dict(request.filters)
    return body


class FleetFinanceReadPort:
    """The port over the governed fleet call of the venue connector."""

    async def _call(self, context: Any, tool: str, arguments: dict[str, Any]) -> Any:
        from arbora.api.invoke.pipeline import OperationRefused
        from arbora.api.ops.nested_invoke import invoke_registered

        reply = await invoke_registered(
            context,
            "fleet.call",
            {"server": VENUE_SERVER, "tool": tool, "arguments": arguments},
        )
        value = reply.get("value", reply) if isinstance(reply, Mapping) else reply
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        if not isinstance(value, Mapping) or set(value) == {"error"}:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the venue did not answer the read"}
            )
        return value

    async def account_binding(self, context: Any, tenant: str) -> str | None:
        answer = await self._call(context, BINDING_TOOL, {})
        account = answer.get("account")
        if answer.get("verified") is True and isinstance(account, str) and account:
            return account
        return None

    async def _read(
        self,
        context: Any,
        method: Literal[
            "list_markets",
            "search_markets",
            "get_market",
            "list_events",
            "search_events",
            "get_event",
            "list_orders",
            "search_orders",
            "get_order",
        ],
        request: ReadRequest,
    ) -> Answer:
        return await self._call(context, TOOLS[method], _arguments(request))

    async def list_markets(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "list_markets", request)

    async def search_markets(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "search_markets", request)

    async def get_market(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "get_market", request)

    async def list_events(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "list_events", request)

    async def search_events(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "search_events", request)

    async def get_event(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "get_event", request)

    async def list_orders(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "list_orders", request)

    async def search_orders(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "search_orders", request)

    async def get_order(self, context: Any, request: ReadRequest) -> Answer:
        return await self._read(context, "get_order", request)


__all__ = ["FinanceReadPort", "FleetFinanceReadPort", "ReadRequest"]
