"""The data-source declaration each connector serves at ``connector://resources``.

ARBORA-FLEET-R041.13: onboarding captures the declaration over MCP and keeps
it here. Nothing reads a connector manifest, a source-presets file or an
ingest-sources file; the served declaration is the only source of a
connector's record-type mappings.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from typing import Any

from agent_connector_sdk.artifacts.resource_descriptor import RESOURCES_URI

__all__ = ["RESOURCES_URI", "declared_mapping", "parse_declaration", "remember"]

_LOCK = threading.Lock()
_DECLARATIONS: dict[str, Mapping[str, Any]] = {}


def parse_declaration(body: str | bytes) -> Mapping[str, Any]:
    """The JSON object a connector served at ``connector://resources``."""
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("connector://resources must be a JSON object")
    return value


def remember(connector: str, declaration: Mapping[str, Any] | None) -> None:
    """Keep the latest captured declaration; ``None`` forgets it."""
    with _LOCK:
        if declaration is None:
            _DECLARATIONS.pop(connector, None)
        else:
            _DECLARATIONS[connector] = declaration


def declaration_of(connector: str) -> Mapping[str, Any] | None:
    with _LOCK:
        return _DECLARATIONS.get(connector)


def declared_mapping(connector: str, record_type: str) -> Mapping[str, Any] | None:
    """The ontology mapping the connector declares for ``record_type``.

    Returns ``None`` when the connector declares no such mapping. Callers
    distinguish "not captured yet" with :func:`declaration_of`.
    """
    declaration = declaration_of(connector)
    mappings = (declaration or {}).get("schema_mappings") or {}
    found = mappings.get(record_type)
    return found if isinstance(found, Mapping) else None
