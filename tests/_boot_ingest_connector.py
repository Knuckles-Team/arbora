"""A fixture connector for the real-boot ingest test: one declared source.

The source is declared in the read tool's ``meta`` and served live at
``connector://resources``, so fleet onboarding captures the
``FixtureItem`` mapping the engine admits records under (SCION-CONNECTOR-CONTROL-R075).
"""

from __future__ import annotations

import sys

from scion_sdk.ingest.live_declaration import (
    register_live_declaration,
    source_meta,
)
from fastmcp import FastMCP

CONNECTOR = "boot-ingest"
connector = FastMCP(CONNECTOR)
ITEMS = [{"id": f"item-{i}", "name": f"Item {i}"} for i in range(1, 4)]


@connector.tool(
    annotations={"readOnlyHint": True},
    meta=source_meta(
        source_id="items",
        record_type="FixtureItem",
        id_field="id",
        title_field="name",
        records_path="data",
        ontology_class="Document",
        fields={"name": "label"},
    ),
)
def boot_ingest_list_items() -> dict[str, object]:
    """List the fixture items."""
    return {"data": ITEMS}


register_live_declaration(connector, CONNECTOR)

if __name__ == "__main__":
    connector.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
