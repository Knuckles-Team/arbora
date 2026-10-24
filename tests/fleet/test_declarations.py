"""Arbora reads a connector's mappings from ``connector://resources`` only (ARBORA-FLEET-R041.13)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.research import _require_declared_mappings
from arbora.fleet import declarations
from arbora.fleet.onboarding import _PackResourceSession, _remember_declaration


class _Listing:
    async def list_resources(self):
        return [
            SimpleNamespace(uri=u)
            for u in (
                "connector://resources",
                "ontology://x",
                "data://live",
                "manifest://connector",
            )
        ]


@pytest.mark.spec("ARBORA-FLEET-R041.11")
def test_capture_lists_the_declaration_and_not_a_manifest() -> None:
    listed = asyncio.run(_PackResourceSession(_Listing()).list_resources())
    assert [r.uri for r in listed] == ["connector://resources", "ontology://x"]


def _pack(body: object) -> SimpleNamespace:
    entry = SimpleNamespace(uri="connector://resources", body=json.dumps(body).encode())
    return SimpleNamespace(archive=SimpleNamespace(entries=[entry]))


@pytest.mark.spec("ARBORA-FLEET-R041.13")
def test_research_mappings_resolve_from_the_captured_declaration() -> None:
    _remember_declaration(
        "web-mcp", _pack({"schema_mappings": {"Document": {}, "Chunk": {}}})
    )
    _require_declared_mappings("web-mcp")  # both declared
    _remember_declaration("web-mcp", _pack({"schema_mappings": {"Document": {}}}))
    with pytest.raises(OperationRefused):
        _require_declared_mappings("web-mcp")
    declarations.remember("web-mcp", None)
    _require_declared_mappings("web-mcp")  # not captured: the engine decides
    assert declarations.declared_mapping("web-mcp", "Document") is None
