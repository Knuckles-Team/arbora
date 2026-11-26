"""RF-ADR-009 gateway — REST host, dashboard, and service aggregation.

CONCEPT:MERISTEM-OS.config.gateway-service-dashboard — Gateway Service Dashboard

Provides data aggregation over the served widget read, and API layer that all
three frontends (overstory, agent-terminal-ui, geniusbot) use to render
Homepage-style service dashboards.

Replaces the former standalone ``service-dashboard-core`` package by folding
its genuinely new functionality (UI models, parallel aggregator, widget ABC)
into the arbora transport host while application behavior remains behind
the explicit ports in :mod:`arbora.gateway.ports`.

Usage::

    from arbora.gateway import Aggregator, ConfigManager
    from arbora.gateway.models import WidgetData, DashboardLayout
"""

from arbora.gateway.models import (
    DashboardLayout,
    ServiceCategory,
    ServiceConfig,
    ServiceGroup,
    WidgetData,
)
from arbora.version import __version__ as __version__

__all__ = [
    "Aggregator",
    "ConfigManager",
    "DashboardLayout",
    "ServiceCategory",
    "ServiceConfig",
    "ServiceGroup",
    "WidgetData",
]
