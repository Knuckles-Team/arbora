"""Arbora reads a connector's mappings from ``connector://resources`` only (ARBORA-FLEET-R041.13)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

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
    # SDK 0.5.9 admits ``data`` as a pack resource scheme, so live data is kept.
    assert [r.uri for r in listed] == [
        "connector://resources",
        "ontology://x",
        "data://live",
    ]


async def _capture(declaration: object) -> Any:
    """Capture a live in-process MCP server through the real SDK 0.5 pack path."""
    from agent_connector_sdk.artifacts.pack import build_content_pack
    from agent_connector_sdk.ports.session import TransportEndpoint
    from agent_connector_sdk.transports.mcp import McpTransport
    from fastmcp import FastMCP

    from arbora.fleet.onboarding import _capture_kinds, _PacedSession

    server: FastMCP[object] = FastMCP("web-mcp", version="1.0.0")

    @server.resource("connector://resources", mime_type="application/json")
    def resources() -> str:
        return json.dumps(declaration)

    @server.resource("ontology://web-mcp/web.ttl", mime_type="text/turtle")
    def ontology() -> str:
        return "@prefix ex: <https://example.org/> . ex:Document a ex:Class ."

    async with McpTransport().session(TransportEndpoint(in_process=server)) as session:
        return await build_content_pack(
            _PacedSession(session, 0.0), connector="web-mcp", kinds=_capture_kinds()
        )


def _pack(body: object) -> Any:
    return asyncio.run(_capture(body))


@pytest.mark.spec("ARBORA-FLEET-R041.15")
def test_sdk_pack_declaration_is_read_from_its_archive_section() -> None:
    """G1: an SDK 0.5 pack's entry body is a PackSection, not bytes."""
    from medulla.generated.connector_pack import PackSection

    declared = {"schema_mappings": {"Document": {}}, "sources": []}
    pack = _pack(declared)
    (entry,) = [
        e for e in pack.archive.entries if str(e.uri) == "connector://resources"
    ]
    assert isinstance(entry.body, PackSection)
    declarations.remember("web-mcp", None)
    _remember_declaration("web-mcp", pack)
    assert declarations.declaration_of("web-mcp") == declared
    declarations.remember("web-mcp", None)


@pytest.mark.spec("ARBORA-FLEET-R041.15")
def test_a_section_that_does_not_match_its_digest_is_refused() -> None:
    pack = _pack({"schema_mappings": {}})
    (entry,) = [
        e for e in pack.archive.entries if str(e.uri) == "connector://resources"
    ]
    tampered = SimpleNamespace(data=b"x" * len(pack.archive.data))
    with pytest.raises(ValueError, match="digest mismatch"):
        declarations.entry_bytes(tampered, entry)


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


@pytest.mark.spec("ARBORA-FLEET-R041.15")
def test_sdk_pack_ontology_bodies_are_read_from_the_archive() -> None:
    from arbora.fleet.access_contracts import pack_ontology_resources

    pack = _pack({"schema_mappings": {}})
    ((uri, text),) = pack_ontology_resources(pack)
    assert uri == "ontology://web-mcp/web.ttl"
    assert text.startswith("@prefix ex:")
