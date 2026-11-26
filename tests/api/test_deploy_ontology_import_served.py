"""The deployment ontology imports once and reads back its served digest (ARBORA-DEPLOY-R009.3)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from tests.api._loopback_engine import loopback  # noqa: F401

TTL = "@prefix o: <urn:ont:> . o:A a o:Class ."
IRI = "urn:ont:deploy"
DIGEST = "sha256:" + hashlib.sha256(TTL.encode()).hexdigest()
SERVED = "sha256:" + "d" * 64


def _params(**more: Any) -> dict[str, Any]:
    return {
        "connector": "arbora",
        "ontology_iri": IRI,
        "version": "1",
        "ontology_ttl": TTL,
        "sha256": DIGEST,
        **more,
    }


def _source(origin: dict[str, Any], sha: str) -> dict[str, Any]:
    return {
        "attached_at_ms": 1,
        "ontology_bytes": len(TTL),
        "ontology_sha256": sha,
        "origin": origin,
        "shapes_bytes": 0,
        "source_id": IRI,
    }


class Schema:
    """A loopback engine's schema authority: a listing and one commit method."""

    def __init__(self, sources: list[dict[str, Any]]) -> None:
        self.sources = sources
        self.commits: list[dict[str, Any]] = []

    def listing(self, params: dict[str, Any], principal: str) -> dict[str, Any]:
        return {
            "composed_digest": SERVED,
            "core_catalog_digest": "c",
            "core_sources": [],
            "dynamic_sources": self.sources,
            "graph": "g",
            "schema_version": 1,
        }

    def commit(self, params: dict[str, Any], principal: str) -> dict[str, Any]:
        self.commits.append(params["op"])
        return {
            "changed": True,
            "composed_digest": SERVED,
            "graph": "g",
            "graph_version": 2,
            "revision": 7,
            "schema_version": 1,
        }


def _serve(loop: Any, schema: Schema) -> None:
    from arbora.api.ops import deploy

    loop.serve_unreleased(
        deploy,
        {"GraphSchemaList": schema.listing, "GraphSchema": schema.commit},
        "GraphSchemaOpImportOntology",
    )


def _run(loop: Any, schema: Schema, key: str) -> Any:
    _serve(loop, schema)
    return loop.call("deploy.ontology.import", _params(), key)


@pytest.mark.spec("ARBORA-DEPLOY-R009.3")
def test_import_reads_back_the_served_digest(loopback: Any) -> None:  # noqa: F811
    schema = Schema([])

    done = _run(loopback, schema, "import-1")

    value = loopback.value(done)
    assert value["sha256"] == DIGEST
    assert value["served_digest"] == SERVED
    assert value["revision"] == 7
    assert [op["op"] for op in schema.commits] == ["import_ontology"]
    assert schema.commits[0]["content_digest"] == DIGEST.removeprefix("sha256:")
    listings = [m for m, _, _ in loopback.engine_calls if m == "GraphSchemaList"]
    assert len(listings) == 2  # before the write, and the read-back after it


@pytest.mark.spec("ARBORA-DEPLOY-R009.3")
def test_drift_and_duplicate_refuse(loopback: Any) -> None:  # noqa: F811
    mine = {"origin": "pack", "connector": "arbora", "record_id": "r"}
    drifted = Schema([_source(mine, "sha256:" + "e" * 64)])
    refused = _run(loopback, drifted, "drift-1")
    assert refused.json()["error"]["code"] == "ONTOLOGY_DRIFT", refused.text
    assert drifted.commits == []

    other = {"origin": "pack", "connector": "someone-else", "record_id": "r"}
    duplicate = Schema([_source(other, DIGEST)])
    _serve(loopback, duplicate)
    refused = loopback.call("deploy.ontology.import", _params(), "dup-1")
    assert refused.json()["error"]["code"] == "ONTOLOGY_DUPLICATE", refused.text
    assert duplicate.commits == []

    # A schema source that does not match its digest never reaches the engine.
    mismatch = loopback.call(
        "deploy.ontology.import", _params(sha256="sha256:" + "0" * 64), "bad-1"
    )
    assert mismatch.status_code >= 400
    assert duplicate.commits == []
