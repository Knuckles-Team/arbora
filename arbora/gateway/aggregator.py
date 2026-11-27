"""Dashboard aggregator: widget data comes from ``ecosystem.widgets.read``.

CONCEPT:MERISTEM-OS.config.gateway-service-dashboard — Gateway Service Dashboard

The aggregator holds no widget code and no widget registry (ARBORA-OPS-R914).
Each configured service names a connector of the fleet census as its
``widget_type``; the aggregator reads it through one served operation, under
the caller's own identity, and projects the reply into ``WidgetData``. It keeps
no cache of its own: the operation softens a down connector with its own
per-tenant rows, and a cache keyed by service id here would cross callers.

Designed for all three frontends:
  - WebUI: called by the dashboard routes, returns JSON
  - TUI/GUI: called with a ``read`` bound to the caller's served operations
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.gateway.config import ConfigManager
from arbora.gateway.models import DashboardLayout, ServiceConfig, WidgetData
from arbora.security.error_surface import public_error_payload

logger = logging.getLogger(__name__)

WIDGET_READ_OP = "ecosystem.widgets.read"

#: One call of ``ecosystem.widgets.read`` as the caller: ``widget`` -> its result.
WidgetReader = Callable[[str], Awaitable[Mapping[str, Any]]]


def widget_data(reply: Mapping[str, Any]) -> WidgetData:
    """Project one ``ecosystem.widgets.read`` result into the dashboard shape."""
    rows = [row for row in reply.get("rows", ()) if isinstance(row, Mapping)]
    keyed = bool(rows) and all("key" in row and "value" in row for row in rows)
    fields: dict[str, Any] = (
        {str(row["key"]): row["value"] for row in rows} if keyed else {"rows": rows}
    )
    degraded = bool(reply.get("degraded"))
    return WidgetData(
        fields=fields,
        status="degraded" if degraded else "ok",
        raw={"source": reply.get("source"), "fetched_at": reply.get("fetched_at")},
    )


class Aggregator:
    """Reads every configured service widget concurrently through ``read``."""

    def __init__(self, config_manager: ConfigManager | None = None) -> None:
        self.config_manager = config_manager or ConfigManager()

    async def fetch_all(self, read: WidgetReader) -> dict[str, WidgetData]:
        """Read every visible configured service; one id maps to one result."""
        layout = self.config_manager.load()
        services = [
            svc for group in layout.groups for svc in group.services if svc.visible
        ]
        fetched = await asyncio.gather(
            *(self._fetch_one(svc, read) for svc in services)
        )
        return {svc.id: data for svc, data in zip(services, fetched, strict=True)}

    async def fetch_one(self, service_id: str, read: WidgetReader) -> WidgetData:
        """Read a single service by id."""
        svc = next(
            (s for s in self.config_manager.get_all_services() if s.id == service_id),
            None,
        )
        if svc is None:
            return WidgetData(status="error", error="service not found")
        return await self._fetch_one(svc, read)

    @staticmethod
    async def _fetch_one(config: ServiceConfig, read: WidgetReader) -> WidgetData:
        try:
            return widget_data(await read(config.widget_type))
        except Exception as exc:
            logger.warning("gateway boundary failed", exc_info=True)
            payload = public_error_payload(exc, logger=logger)
            return WidgetData(
                status="error",
                error=payload["message"],
                raw={
                    "code": payload["code"],
                    "correlation_id": payload["correlation_id"],
                },
            )

    async def health_check(self, read: WidgetReader) -> dict[str, bool]:
        """A service is healthy when its widget reads without degrading."""
        results = await self.fetch_all(read)
        return {sid: data.status == "ok" for sid, data in results.items()}

    def get_layout(self) -> DashboardLayout:
        """The current dashboard layout."""
        return self.config_manager.load()

    def save_layout(self, layout: DashboardLayout) -> None:
        """Save the dashboard layout to YAML."""
        self.config_manager.save(layout)
