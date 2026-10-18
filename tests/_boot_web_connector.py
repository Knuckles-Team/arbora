"""A fixture web-source connector for the real-boot test (ARBORA-OPS-R067.3).

It serves the web-source tool ``web_extract`` over streamable HTTP and the
``manifest://connector`` resource with ``Document`` and ``Chunk``
``schema_mappings``, so the pack Arbora's fleet onboarding imports lets
``SourceIngest`` resolve ``manifest:<connector>#schema_mappings/<type>``.
``web_extract`` reads a page from the test's fixture page server and returns
its paragraphs as text.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sys
import urllib.request

from fastmcp import FastMCP

NAME = sys.argv[2] if len(sys.argv) > 2 else "webarchiver-mcp"
connector = FastMCP(NAME)

_S, _I = "xsd:string", "xsd:integer"
MANIFEST = {
    "connector": NAME,
    "ontology_source": "webarchiver",
    "schema_version": "1",
    "resources": [{"name": "Document"}, {"name": "Chunk"}],
    "identity": {
        "id_field": {"Document": "id", "Chunk": "id"},
        "title_field": {"Document": "title", "Chunk": "title"},
    },
    "schema_mappings": {
        "Document": {
            "ontology_class": "Document",
            "fields": {
                **dict.fromkeys(
                    (
                        "text title source_uri content_hash url canonical_url author"
                        " published site_name content_sha256 backend fetch_mode"
                        " fallback_from search_query search_engine"
                    ).split(),
                    _S,
                ),
                "search_rank": _I,
                "chunk_count": _I,
            },
        },
        "Chunk": {
            "ontology_class": "Chunk",
            "fields": {
                **dict.fromkeys(
                    "text title source_uri content_hash document_id backend".split(), _S
                ),
                "chunk_index": _I,
                "char_start": _I,
                "char_end": _I,
            },
        },
    },
    "sync": [],
    "provenance": {
        "generated_by": "tests/_boot_web_connector.py",
        "source_artifacts": [],
        "integrity": {"algorithm": "sha256", "hash": "0" * 64},
    },
}


@connector.resource("manifest://connector", mime_type="application/json")
def manifest() -> str:
    """The connector manifest."""
    return json.dumps(MANIFEST)


@connector.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def web_extract(url: str) -> dict:
    """Fetch one fixture page and return its main text and metadata."""
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - loopback fixture
        html = response.read().decode("utf-8")
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    text = "\n".join(
        re.sub(r"<[^>]+>", "", p).strip()
        for p in re.findall(r"<p>(.*?)</p>", html, re.S)
    )
    return {
        "backend": "webarchiver",
        "fetch_mode": "http",
        "url": url,
        "final_url": url,
        "status": 200,
        "content_digest": hashlib.sha256(text.encode()).hexdigest(),
        "retrieved_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "page": {
            "title": title.group(1) if title else "",
            "canonical_url": url,
            "text": text,
            "author": "Fixture",
        },
    }


if __name__ == "__main__":
    connector.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[1]))
