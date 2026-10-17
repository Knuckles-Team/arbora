"""Ollama widget — local LLM model serving."""

from __future__ import annotations

import logging

from arbora.gateway.models import (
    ServiceCategory,
    ServiceConfig,
    WidgetData,
    WidgetField,
)
from arbora.gateway.widgets.base import BaseWidget

logger = logging.getLogger(__name__)


class Widget(BaseWidget):
    service_type = "ollama"
    display_name = "Ollama"
    icon = "cpu"
    category = ServiceCategory.DATA_SCIENCE
    description = "Local LLM — models, running processes, and GPU usage"
    env_prefix = "OLLAMA"

    def get_fields(self) -> list[WidgetField]:
        return self.get_widget_fields("ollama")

    def fetch_data(self, config: ServiceConfig) -> WidgetData:
        from arbora.core.transport import create_http_client

        url = self._resolve_url(config)
        client = create_http_client(timeout=5.0)
        try:
            resp = client.request("GET", f"{url}/api/tags")
            data = resp.json() if resp.status_code == 200 else {}
            models = data.get("models", [])
            ps_resp = client.request("GET", f"{url}/api/ps")
            ps = ps_resp.json() if ps_resp.status_code == 200 else {}
            running = ps.get("models", [])
        except Exception as e:
            logger.warning("Ollama fetch: %s", e)
            return self._error_data(e)
        finally:
            client.close()

        return WidgetData(
            fields={
                "models": len(models),
                "running": len(running),
                "status": "Online",
            },
            status="ok",
        )
