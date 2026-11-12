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

from scion_sdk.manifest.resource_descriptor import RESOURCES_URI

__all__ = [
    "RESOURCES_URI",
    "Declarations",
    "entry_bytes",
    "parse_declaration",
]


def entry_bytes(archive: Any, entry: Any) -> bytes:
    """The bytes of one captured pack entry's body, read from its archive.

    A captured ConnectorPack (scion-sdk 0.5) holds every body in one
    archive blob; ``entry.body`` is the ``PackSection`` (offset, length,
    sha256) that locates it. A section outside the archive or whose bytes do
    not hash to its digest is refused, never read as content.
    """
    import hashlib

    section = entry.body
    data = archive.data
    start, length = int(section.offset), int(section.length)
    if start < 0 or length < 0 or start + length > len(data):
        raise ValueError(f"{entry.uri}: body section lies outside the archive")
    body = bytes(data[start : start + length])
    if hashlib.sha256(body).hexdigest() != str(section.sha256):
        raise ValueError(f"{entry.uri}: body section digest mismatch")
    return body


def parse_declaration(body: str | bytes) -> Mapping[str, Any]:
    """The JSON object a connector served at ``connector://resources``."""
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("connector://resources must be a JSON object")
    return value


class Declarations:
    """The latest captured declaration per connector, for one fleet (ARBORA-HOST-R039.2)."""

    __slots__ = ("_declarations", "_lock")

    def __init__(self) -> None:
        self._declarations: dict[str, Mapping[str, Any]] = {}
        self._lock = threading.Lock()

    def remember(self, connector: str, declaration: Mapping[str, Any] | None) -> None:
        """Keep the latest captured declaration; ``None`` forgets it."""
        with self._lock:
            if declaration is None:
                self._declarations.pop(connector, None)
            else:
                self._declarations[connector] = declaration

    def declaration_of(self, connector: str) -> Mapping[str, Any] | None:
        with self._lock:
            return self._declarations.get(connector)

    def declared_mapping(
        self, connector: str, record_type: str
    ) -> Mapping[str, Any] | None:
        """The ontology mapping the connector declares for ``record_type``.

        Returns ``None`` when the connector declares no such mapping. Callers
        distinguish "not captured yet" with :meth:`declaration_of`.
        """
        declaration = self.declaration_of(connector)
        mappings = (declaration or {}).get("schema_mappings") or {}
        found = mappings.get(record_type)
        return found if isinstance(found, Mapping) else None
