"""Widget Registry — auto-discovers and manages service widgets.

CONCEPT:MERISTEM-OS.config.gateway-service-dashboard — Gateway Service Dashboard

Mirrors Homepage's ``widgets.js`` / ``components.js`` registry pattern
but uses Python's import mechanism for discovery. Now lives inside
``agent-utilities`` rather than the former standalone package.
"""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING

from arbora.gateway.models import WidgetRegistration

if TYPE_CHECKING:
    from arbora.gateway.widgets.base import BaseWidget

logger = logging.getLogger(__name__)

# Map of widget_type -> module path within arbora.gateway.widgets
_BUILTIN_WIDGETS: dict[str, str] = {
    "portainer": "arbora.gateway.widgets.portainer",
    "uptime_kuma": "arbora.gateway.widgets.uptime_kuma",
    "technitium": "arbora.gateway.widgets.technitium",
    "caddy": "arbora.gateway.widgets.caddy",
    "gitlab": "arbora.gateway.widgets.gitlab",
    "jellyfin": "arbora.gateway.widgets.jellyfin",
    "qbittorrent": "arbora.gateway.widgets.qbittorrent",
    "nextcloud": "arbora.gateway.widgets.nextcloud",
    "home_assistant": "arbora.gateway.widgets.home_assistant",
    "mealie": "arbora.gateway.widgets.mealie",
    "container_manager": "arbora.gateway.widgets.container_manager",
    "mattermost": "arbora.gateway.widgets.mattermost",
    "keycloak": "arbora.gateway.widgets.keycloak",
    "openbao": "arbora.gateway.widgets.openbao",
    "langfuse": "arbora.gateway.widgets.langfuse",
    "plane": "arbora.gateway.widgets.plane",
    "servicenow": "arbora.gateway.widgets.servicenow",
    "erpnext": "arbora.gateway.widgets.erpnext",
    "wger": "arbora.gateway.widgets.wger",
    "owncast": "arbora.gateway.widgets.owncast",
    "github": "arbora.gateway.widgets.github",
    "searxng": "arbora.gateway.widgets.searxng",
    "media_downloader": "arbora.gateway.widgets.media_downloader",
    "stirlingpdf": "arbora.gateway.widgets.stirlingpdf",
    "microsoft": "arbora.gateway.widgets.microsoft",
    "postiz": "arbora.gateway.widgets.postiz",
    "archivebox": "arbora.gateway.widgets.archivebox",
    "leanix": "arbora.gateway.widgets.leanix",
    "listmonk": "arbora.gateway.widgets.listmonk",
    "tunnel_manager": "arbora.gateway.widgets.tunnel_manager",
    "repository_manager": "arbora.gateway.widgets.repository_manager",
    "systems_manager": "arbora.gateway.widgets.systems_manager",
    "data_science": "arbora.gateway.widgets.data_science",
    "vector_db": "arbora.gateway.widgets.vector_db",
    "documentdb": "arbora.gateway.widgets.documentdb",
    "scholarx": "arbora.gateway.widgets.scholarx",
    "audio_transcriber": "arbora.gateway.widgets.audio_transcriber",
    "arr": "arbora.gateway.widgets.arr",
    "ansible_tower": "arbora.gateway.widgets.ansible_tower",
    "lgtm": "arbora.gateway.widgets.lgtm",
    "emerald_exchange": "arbora.gateway.widgets.emerald_exchange",
    "legal_peripherals": "arbora.gateway.widgets.legal_peripherals",
    "twenty": "arbora.gateway.widgets.twenty",
    "orchestrator": "arbora.gateway.widgets.orchestrator",
    "atlassian": "arbora.gateway.widgets.atlassian",
    "google_workspace": "arbora.gateway.widgets.google_workspace",
    "zulip": "arbora.gateway.widgets.zulip",
    "teleport": "arbora.gateway.widgets.teleport",
    "sentry": "arbora.gateway.widgets.sentry",
    "ollama": "arbora.gateway.widgets.ollama",
    # Connector-fleet expansion (widget-connector-expansion) — one entry per
    # agent-packages/agents/* connector that exposes a usable programmatic
    # client. See
    # docs/pillars/5_agent_os_infrastructure/OS-5.9-Gateway_Service_Dashboard.md
    # for the full connector<->widget inventory and the connectors excluded
    # (no single-service status to poll, or no simple url+token client shape).
    "aris": "arbora.gateway.widgets.aris",
    "audiobookshelf": "arbora.gateway.widgets.audiobookshelf",
    "camunda": "arbora.gateway.widgets.camunda",
    "ciso_assistant": "arbora.gateway.widgets.ciso_assistant",
    "clarity": "arbora.gateway.widgets.clarity",
    "dockerhub": "arbora.gateway.widgets.dockerhub",
    "egeria": "arbora.gateway.widgets.egeria",
    "fan_manager": "arbora.gateway.widgets.fan_manager",
    "firefly_iii": "arbora.gateway.widgets.firefly_iii",
    "freshrss": "arbora.gateway.widgets.freshrss",
    "gramps": "arbora.gateway.widgets.gramps",
    "hdhomerun": "arbora.gateway.widgets.hdhomerun",
    "jena": "arbora.gateway.widgets.jena",
    "kafka": "arbora.gateway.widgets.kafka",
    "okta": "arbora.gateway.widgets.okta",
    "onetrust": "arbora.gateway.widgets.onetrust",
    "paperless_ngx": "arbora.gateway.widgets.paperless_ngx",
    "pulselink": "arbora.gateway.widgets.pulselink",
    "rom_manager": "arbora.gateway.widgets.rom_manager",
}


class Registry:
    """Global registry of available service widgets.

    Provides lazy loading — widgets are only imported when first accessed.
    Connector packages are never imported; live availability comes from the
    Medulla-backed fleet catalog when the widget delegates its MCP operation.
    """

    def __init__(self) -> None:
        self._widgets: dict[str, type[BaseWidget]] = {}
        self._registrations: dict[str, WidgetRegistration] = {}
        self._loaded: set[str] = set()

    def discover_all(self) -> dict[str, WidgetRegistration]:
        """Attempt to load all builtin widgets and return their registrations.

        Module-load failures are isolated. Connector runtime availability is
        evaluated later through the served fleet catalog, not Python imports.
        """
        for widget_type, module_path in _BUILTIN_WIDGETS.items():
            if widget_type not in self._loaded:
                self._try_load(widget_type, module_path)
        return dict(self._registrations)

    def get_widget(self, widget_type: str) -> BaseWidget | None:
        """Get an instantiated widget by type key.

        Lazily imports the widget module on first access.
        """
        if widget_type not in self._widgets:
            module_path = _BUILTIN_WIDGETS.get(widget_type)
            if not module_path:
                logger.warning("Unknown widget type: %s", widget_type)
                return None
            self._try_load(widget_type, module_path)

        widget_cls = self._widgets.get(widget_type)
        if widget_cls:
            return widget_cls()
        return None

    def get_registration(self, widget_type: str) -> WidgetRegistration | None:
        """Get widget registration metadata without instantiating."""
        if widget_type not in self._registrations:
            self.get_widget(widget_type)  # Trigger lazy load
        return self._registrations.get(widget_type)

    def list_available(self) -> list[str]:
        """List all widget types that can be loaded (deps installed)."""
        self.discover_all()
        return list(self._registrations.keys())

    def list_all_known(self) -> list[str]:
        """List all known widget types (including those with missing deps)."""
        return list(_BUILTIN_WIDGETS.keys())

    def _try_load(self, widget_type: str, module_path: str) -> None:
        """Attempt to import a widget module. Silently skip on ImportError."""
        self._loaded.add(widget_type)
        try:
            module = importlib.import_module(module_path)
            widget_cls = getattr(module, "Widget", None)
            if widget_cls is None:
                logger.debug(
                    "Widget module %s has no 'Widget' class, skipping", module_path
                )
                return

            self._widgets[widget_type] = widget_cls

            # Build registration from widget class attributes
            instance = widget_cls()
            self._registrations[widget_type] = WidgetRegistration(
                widget_type=instance.service_type,
                display_name=instance.display_name,
                icon=instance.icon,
                category=instance.category,
                description=instance.description,
                available_fields=instance.get_fields(),
                supports_websocket=getattr(instance, "supports_websocket", False),
                env_prefix=getattr(instance, "env_prefix", ""),
            )
            logger.debug("Loaded widget: %s (%s)", widget_type, instance.display_name)

        except ImportError as exc:
            logger.warning("Widget %s skipped: %s", widget_type, exc)
        except Exception as exc:
            logger.warning(
                "Widget %s failed to load (exception_type=%s)",
                widget_type,
                exc,
            )


# Global singleton
_registry: Registry | None = None


def get_registry() -> Registry:
    """Get or create the global widget registry singleton."""
    global _registry
    if _registry is None:
        _registry = Registry()
    return _registry
