"""The one reader of a server's tool rows (ARBORA-FLEET-R073.2).

Every site that lists, counts or filters one server's tools reads them through a
``ToolRowReader``. The reader is the seam the engine-paged read replaces: today
``ProbeToolRows`` answers from the rows the multiplexer's own probe produced; the
engine reader pages them by server and name through component search, behind the
pin-lag guard ``require_component_search_filters``. Callers hold no second copy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ToolRowReader(Protocol):
    """Read one server's tool rows: ``{name, description, inputSchema, ...}``."""

    async def rows(self, server: str, info: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Every tool row of ``server``, in the order its probe listed them."""
        ...

    async def count(self, server: str, info: Mapping[str, Any]) -> int:
        """How many tools ``server`` serves; reads no row body."""
        ...


class ProbeToolRows:
    """The rows of the probe result the caller already holds, copied per read."""

    async def rows(self, server: str, info: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(item) for item in info.get("tools") or () if isinstance(item, Mapping)
        ]

    async def count(self, server: str, info: Mapping[str, Any]) -> int:
        return len(info.get("tools") or ())


__all__ = ["ProbeToolRows", "ToolRowReader"]
