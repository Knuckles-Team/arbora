"""Audiobookshelf widget — audiobook/podcast library server."""

from __future__ import annotations

import logging

from arbora.gateway.models import (
    ServiceCategory,
    ServiceConfig,
    WidgetData,
    WidgetField,
)
from arbora.gateway.widgets.base import BaseWidget
from arbora.gateway.widgets.fleet_client import count_items

logger = logging.getLogger(__name__)


class Widget(BaseWidget):
    service_type = "audiobookshelf"
    display_name = "Audiobookshelf"
    icon = "book-audio"
    category = ServiceCategory.MEDIA
    description = "Audiobook and podcast library — libraries and item counts"
    env_prefix = "AUDIOBOOKSHELF"

    def get_fields(self) -> list[WidgetField]:
        return [
            WidgetField(key="libraries", label="Libraries", format="number"),
            WidgetField(key="status", label="Status", format="text"),
        ]

    def fetch_data(self, config: ServiceConfig) -> WidgetData:
        client = self._fleet_client()

        try:
            libraries = client.get_libraries() or {}
        except Exception as e:
            return self._error_data(e)

        return WidgetData(
            fields={
                "libraries": count_items(libraries, key="libraries"),
                "status": "Connected",
            },
            status="ok",
        )
