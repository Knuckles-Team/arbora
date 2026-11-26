"""The market compute port (ARBORA-DATA-MARKET-R003.13).

Every figure of a sizing or shift calculation is computed by the engine. The
port sends one allowlisted ``Finance*`` method through an engine client held
by the composition root and returns the engine's answer as plain data. It
holds no formula and no default: an omitted optional stays omitted, so the
engine applies its own knobs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

__all__ = ["ALLOWED_METHODS", "EngineMarketComputePort", "MarketComputePort"]

#: The only methods this port sends.
ALLOWED_METHODS = frozenset({"FinancePositionSize", "FinanceDistributionShift"})

ClientFor = Callable[[str], Awaitable[Any]]


class MarketComputePort(Protocol):
    """One engine calculation for one verified tenant."""

    async def call(self, method: str, params: Mapping[str, Any], *, tenant: str) -> Any:
        """Send ``method`` and return the engine's answer as plain data."""


class EngineMarketComputePort:
    """The port over the engine client of the composition root."""

    def __init__(self, client_for: ClientFor, graph: str) -> None:
        self._client_for = client_for
        self._graph = graph

    async def call(self, method: str, params: Mapping[str, Any], *, tenant: str) -> Any:
        from medulla import EngineResponseError
        from medulla.generated import METHOD_IDS, send_by_id

        from arbora.api.errors import EngineRefusal
        from arbora.api.ops._engine import decode, refusal_code, unavailable

        if method not in ALLOWED_METHODS:
            raise unavailable("the market compute port does not send this method")
        if method not in METHOD_IDS:
            raise unavailable(f"the installed engine does not serve {method}")
        client = await self._client_for(tenant)
        try:
            payload = await send_by_id(client, method, dict(params), self._graph)
        except EngineResponseError as exc:
            raise EngineRefusal(refusal_code(exc.code, exc.detail)) from exc
        return decode(payload)
