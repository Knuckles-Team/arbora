"""RF-ADR-009 gateway — REST host, dashboard, and service aggregation.

CONCEPT:MERISTEM-OS.config.gateway-service-dashboard — Gateway Service Dashboard

Provides the widget registry, data aggregation, and API layer that all
three frontends (overstory, agent-terminal-ui, geniusbot) use to render
Homepage-style service dashboards.

Replaces the former standalone ``service-dashboard-core`` package by folding
its genuinely new functionality (UI models, parallel aggregator, widget ABC)
into the arbora transport host while application behavior remains behind
the explicit ports in :mod:`arbora.gateway.ports`.

Usage::

    from arbora.gateway import Aggregator, ConfigManager
    from arbora.gateway.models import WidgetData, DashboardLayout
    from arbora.gateway.registry import get_registry
"""

from arbora._version import __version__ as __version__
from arbora.gateway.models import (
    DashboardLayout,
    ServiceCategory,
    ServiceConfig,
    ServiceGroup,
    WidgetData,
    WidgetField,
    WidgetRegistration,
)
from arbora.gateway.registry import Registry, get_registry

__all__ = [
    "Aggregator",
    "ConfigManager",
    "DashboardLayout",
    "Registry",
    "ServiceCategory",
    "ServiceConfig",
    "ServiceGroup",
    "WidgetData",
    "WidgetField",
    "WidgetRegistration",
    "get_registry",
]
