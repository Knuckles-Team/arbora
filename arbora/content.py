"""Arbora-owned declarative content provider."""

from __future__ import annotations

from pathlib import Path

from agent_connector_sdk.mcp.content import ConnectorContent

from arbora._version import __version__


def connector_content() -> ConnectorContent:
    """Return Arbora's independently identified SDK content provider."""

    return ConnectorContent(
        connector="arbora",
        package_root=Path(__file__).resolve().parent,
        package_version=__version__,
    )


__all__ = ["connector_content"]
