"""A fixture connector for the real-boot ingest test: one declared source.

It serves the SDK's read-only ``boot_ingest_ingest_sources`` declaration tool,
the read-only ``boot_ingest_list_items`` tool the source pages through, and its
connector manifest as ``manifest://connector`` so fleet onboarding imports the
``schema_mappings/FixtureItem`` mapping the engine admits records under.
"""

from __future__ import annotations

import sys
from pathlib import Path

from agent_connector_sdk.ingest.sources import (
    IngestSourceDeclaration,
    register_ingest_sources_tool,
)
from fastmcp import FastMCP
from fastmcp.resources import FileResource
from pydantic import AnyUrl

CONNECTOR = "boot-ingest"
connector = FastMCP(CONNECTOR)
ITEMS = [{"id": f"item-{i}", "name": f"Item {i}"} for i in range(1, 4)]


@connector.tool(annotations={"readOnlyHint": True})
def boot_ingest_list_items() -> dict[str, object]:
    """List the fixture items."""
    return {"data": ITEMS}


register_ingest_sources_tool(
    connector,
    "boot_ingest",
    IngestSourceDeclaration.model_validate(
        {
            "connector": CONNECTOR,
            "sources": [
                {
                    "source_id": "items",
                    "record_type": "FixtureItem",
                    "tools": ["boot_ingest_list_items"],
                    "records_path": "data",
                    "id_field": "id",
                    "title_field": "name",
                }
            ],
        }
    ),
)

_MANIFEST = Path(__file__).with_name("_boot_ingest_manifest.yml")
connector.add_resource(
    FileResource(
        uri=AnyUrl("manifest://connector"),
        path=_MANIFEST.resolve(),
        name="manifest://connector",
        mime_type="text/yaml",
    )
)

if __name__ == "__main__":
    connector.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
