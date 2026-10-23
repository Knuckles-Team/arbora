"""A connector's data-resource declaration does not fail onboarding (ARBORA-FLEET-R041.11)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_connector_sdk.artifacts.resources import ResourceArtifactKind
from agent_connector_sdk.contracts import ServerIdentity

MANIFEST = (Path(__file__).parent.parent / "_boot_ingest_manifest.yml").read_text(
    "utf-8"
)


class _Session:
    def __init__(self, bodies: dict[str, tuple[str, str]]) -> None:
        self._bodies = bodies

    async def list_resources(self):
        return [
            SimpleNamespace(uri=uri, name=uri, mimeType=mime)
            for uri, (mime, _) in self._bodies.items()
        ]

    async def read_resource(self, uri):
        return self._bodies[str(uri)][1]


@pytest.mark.spec("ARBORA-FLEET-R041.11")
@pytest.mark.asyncio
async def test_resources_declaration_is_not_parsed_as_the_manifest() -> None:
    declaration = {"connector": "fixture-mcp", "resources": []}
    session = _Session(
        {
            "manifest://connector": ("application/yaml", MANIFEST),
            "manifest://connector/resources": (
                "application/json",
                json.dumps(declaration),
            ),
        }
    )
    kind = ResourceArtifactKind()
    server = ServerIdentity(name="fixture-mcp", version="1.0.0")
    entries = await kind.list_entries(session, server)
    for entry in entries:
        kind.validate(entry)  # the capture validates every resource body
    assert sorted(e.uri for e in entries) == [
        "manifest://connector",
        "manifest://connector/resources",
    ]
