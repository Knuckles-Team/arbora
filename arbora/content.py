"""Everything arbora serves as content, found through the connector SDK.

Arbora's own package, the skills packages and every content-only connector
declare themselves once under ``agent_connector_sdk.connector``;
:func:`content_providers` turns each installed declaration into the SDK's
:class:`~agent_connector_sdk.mcp.content.ConnectorContent`, and
:func:`register_installed_content` serves them all with the SDK's own
registration (``ARBORA-FLEET-R044``):

* ``skills/<name>/SKILL.md`` as ``skill://<name>/SKILL.md`` through fastmcp's
  ``SkillsDirectoryProvider``;
* ``prompts/<name>.json`` as native fastmcp prompts with title, tags and meta;
* ``ontology/*.ttl`` and ``ontology/shapes/*.ttl`` as ``ontology://`` and
  ``shapes://`` resources.

The engine's core ontology and shapes are served from the installed
epistemic-graph package data under the fleet name ``epistemic-graph``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from agent_connector_sdk.mcp.content import (
    ConnectorContent,
    ContentRegistration,
    register_connector_content,
)
from fastmcp.resources import FileResource
from pydantic import AnyUrl

from arbora.core.providers import content_connectors

logger = logging.getLogger(__name__)

ContentProvider = Callable[[], ConnectorContent]

#: The package and directory in which the engine wheel ships its core ontology.
EG_PACKAGE = "epistemic_graph"
EG_ONTOLOGY_DIR = "ontology"
EG_CONNECTOR = "epistemic-graph"
_SHAPES_SUFFIX = ".shapes.ttl"


def content_providers() -> tuple[ContentProvider, ...]:
    """One provider per installed package that declares content."""
    return tuple(
        (
            lambda r=resolved: ConnectorContent(
                connector=r.name, package_root=r.dirs.root, package_version=r.version
            )
        )
        for resolved in content_connectors()
    )


def eg_core_ontology_resources(
    package: str = EG_PACKAGE, directory: str = EG_ONTOLOGY_DIR
) -> list[FileResource]:
    """The engine's core ontology and shapes, read from its package data.

    ``<dir>/*.ttl`` become ``ontology://epistemic-graph/<file>``; files named
    ``*.shapes.ttl`` and files under ``<dir>/shapes/`` become ``shapes://``.
    An engine wheel that ships no ontology data yields no resources and the
    absence is logged by name.
    """
    try:
        root = Path(str(resources.files(package) / directory))
    except (ModuleNotFoundError, TypeError):
        root = Path("/nonexistent")
    if not root.is_dir():
        logger.warning(
            "%s ships no %s data; no core ontology is served", package, directory
        )
        return []
    entries = [
        ("shapes" if path.name.endswith(_SHAPES_SUFFIX) else "ontology", path)
        for path in sorted(root.glob("*.ttl"))
    ] + [("shapes", path) for path in sorted((root / "shapes").glob("*.ttl"))]
    return [
        FileResource(
            uri=AnyUrl(f"{scheme}://{EG_CONNECTOR}/{path.name}"),
            path=path.resolve(),
            name=f"{scheme}://{EG_CONNECTOR}/{path.name}",
            mime_type="text/turtle",
        )
        for scheme, path in entries
    ]


@dataclass(frozen=True)
class InstalledContentRegistration:
    """What :func:`register_installed_content` served."""

    connectors: dict[str, ContentRegistration]
    core_ontology_resources: int


def register_installed_content(
    mcp: Any, providers: tuple[ContentProvider, ...] | None = None
) -> InstalledContentRegistration:
    """Serve every installed package's content and the engine core ontology.

    Duplicate skill identities across packages are reported by name when the
    skill catalog is built (``arbora.core.providers``).
    """
    registered: dict[str, ContentRegistration] = {}
    for provider in providers if providers is not None else content_providers():
        content = provider()
        registered[content.connector] = register_connector_content(mcp, content)
    core = eg_core_ontology_resources()
    for resource in core:
        mcp.add_resource(resource)
    return InstalledContentRegistration(registered, len(core))


__all__ = [
    "ContentProvider",
    "InstalledContentRegistration",
    "content_providers",
    "eg_core_ontology_resources",
    "register_installed_content",
]
