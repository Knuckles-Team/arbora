"""The market compute port (ARBORA-DATA-MARKET-R003.13).

``market.position_size`` and ``market.distribution_shift`` reach the engine's
``FinancePositionSize`` and ``FinanceDistributionShift`` through one port. The port
sends as the verified caller and returns the engine's answer unchanged; it holds no
formula and adds no default, so an omitted optional stays omitted and the engine
applies its own knobs. Without the port the operations refuse ``UNAVAILABLE``.
"""

from __future__ import annotations

from typing import Any, Protocol

from arbora.api.ops._engine import engine_send

__all__ = ["COMPUTE_METHODS", "EngineMarketComputePort", "MarketComputePort"]

#: The only engine methods this port sends.
COMPUTE_METHODS = frozenset({"FinancePositionSize", "FinanceDistributionShift"})


class MarketComputePort(Protocol):
    async def call(self, context: Any, method: str, params: dict[str, Any]) -> Any: ...


class EngineMarketComputePort:
    """The port over the verified caller's engine client."""

    async def call(self, context: Any, method: str, params: dict[str, Any]) -> Any:
        if method not in COMPUTE_METHODS:
            raise ValueError(f"not a market compute method: {method}")
        return await engine_send(context, method, params)
