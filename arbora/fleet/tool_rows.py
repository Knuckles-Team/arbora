"""The one reader of a server's tool rows (ARBORA-FLEET-R073.2).

Every site that lists, counts or filters one server's tools reads them through a
``ToolRowReader``. Two implementations exist, chosen once by the pin-lag guard
``tool_row_reader``:

* ``EngineToolRows`` pages the rows from the engine by server name through component
  search (R073.1). The process keeps a count and one digest per probed server, never
  the rows, so its resident catalog state is flat in catalog size.
* ``ProbeToolRows`` answers from the probe result the caller holds. It is the process
  copy the pinned release still needs, and it is deleted with the re-pin that carries
  the engine search filters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable

#: One engine search page, and the bound on pages a single server read follows.
PAGE = 100
MAX_PAGES = 1000


@runtime_checkable
class ToolRowReader(Protocol):
    """Read one server's tool rows: ``{name, description, inputSchema, ...}``."""

    async def rows(
        self, server: str, info: Mapping[str, Any], *, schemas: bool = False
    ) -> list[dict[str, Any]]:
        """Every tool row of ``server``; ``schemas`` also reads each input schema."""
        ...

    async def count(self, server: str, info: Mapping[str, Any]) -> int:
        """How many tools ``server`` serves; reads no row body."""
        ...

    def resident(self, info: dict[str, Any]) -> dict[str, Any]:
        """What the process keeps of one probe result (the same object, slimmed or not)."""
        ...


class ProbeToolRows:
    """The rows of the probe result the caller already holds, copied per read."""

    async def rows(
        self, server: str, info: Mapping[str, Any], *, schemas: bool = False
    ) -> list[dict[str, Any]]:
        return [
            dict(item) for item in info.get("tools") or () if isinstance(item, Mapping)
        ]

    async def count(self, server: str, info: Mapping[str, Any]) -> int:
        return len(info.get("tools") or ())

    def resident(self, info: dict[str, Any]) -> dict[str, Any]:
        return info


def tools_digest(rows: list[Mapping[str, Any]]) -> str:
    """One digest of a server's tool names and descriptions, order independent."""
    material = sorted((str(r.get("name")), str(r.get("description", ""))) for r in rows)
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


class EngineToolRows:
    """Tool rows paged from the engine by server name, never held in the process."""

    def __init__(
        self,
        port: Any,
        *,
        tenant_id: str,
        scope: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        self._port = port
        self._tenant_id = tenant_id
        self._scope = scope

    async def _entries(self, server: str) -> list[Any]:
        from arbora.fleet.catalog_reader import ComponentSearchRequest

        entries: list[Any] = []
        cursor: Any = None
        with self._scope():
            for _ in range(MAX_PAGES):
                page = await self._port.search_components(
                    ComponentSearchRequest(
                        tenant_id=self._tenant_id,
                        kinds=("tool",),
                        limit=PAGE,
                        cursor=cursor,
                        server_name=server,
                    )
                )
                entries.extend(page.entries)
                cursor = page.next_cursor
                if cursor is None:
                    return entries
        raise RuntimeError(f"the engine tool pages of {server} exceed the page bound")

    async def _schema(self, entry: Any) -> dict[str, Any]:
        from arbora.fleet.catalog_items import _engine_input_schema

        with self._scope():
            content = await self._port.component_content(
                tenant_id=self._tenant_id,
                component_id=entry.component_id,
                entry_revision=entry.entry_revision,
            )
        return _engine_input_schema(content.body)

    async def rows(
        self, server: str, info: Mapping[str, Any], *, schemas: bool = False
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for entry in await self._entries(server):
            row: dict[str, Any] = {
                "name": entry.upstream_name,
                "description": entry.summary,
            }
            if schemas:
                row["inputSchema"] = await self._schema(entry)
            rows.append(row)
        return rows

    async def count(self, server: str, info: Mapping[str, Any]) -> int:
        return len(await self._entries(server))

    def resident(self, info: dict[str, Any]) -> dict[str, Any]:
        """Keep the count and one digest; the rows are the engine's."""
        rows = [r for r in info.get("tools") or () if isinstance(r, Mapping)]
        info["tool_count"] = len(rows)
        info["tools_digest"] = tools_digest(rows)
        info["tools"] = []
        return info


def tool_row_reader(
    port: Any, *, tenant_id: str, scope: Callable[[], AbstractContextManager[Any]]
) -> ToolRowReader:
    """The engine reader once the pinned release has the search filters, else the probe's.

    PIN-LAG (``require_component_search_filters``): delete the probe branch, and
    ``ProbeToolRows``, with the re-pin that carries ``server_name`` and ``upstream_name``.
    """
    from arbora.pin_lag.medulla import component_search_filters_served

    if component_search_filters_served():
        return EngineToolRows(port, tenant_id=tenant_id, scope=scope)
    return ProbeToolRows()


__all__ = [
    "EngineToolRows",
    "ProbeToolRows",
    "ToolRowReader",
    "tool_row_reader",
    "tools_digest",
]
