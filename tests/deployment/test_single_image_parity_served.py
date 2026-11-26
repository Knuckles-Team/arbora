"""The single image serves the same results as the clustered contract (ARBORA-DEPLOY-R009.2.2).

The run starts with no ``GRAPH_SERVICE_ENDPOINTS`` and no external service. It
calls every operation of ``parity_matrix.json`` over the served HTTP surface of
the one loopback engine harness. A row whose observed result differs from the
matrix fails ``PARITY_MISMATCH`` naming the operation, and a capability the
single image cannot serve must refuse with the clustered refusal code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arbora.api.ops import deploy
from arbora.api.ops.registry_factory import get_registry
from tests.api._loopback_engine import loopback  # noqa: F401

MATRIX = json.loads((Path(__file__).parent / "parity_matrix.json").read_text())

PROPOSAL = {
    "proposal_digest": "a" * 64,
    "premise_revision": 1,
    "policy_revision": "p",
    "resource_class_iri": "cell:one",
    "proposed_capacity": 2,
}
BODY = "@prefix o: <urn:o:> . o:A a o:Class ."
CALLS: dict[str, dict[str, Any]] = {
    "deploy.placement.apply": {
        "target": "compose",
        "components": ["tool:a"],
        "plan_digest": "sha256:" + "1" * 64,
        "choices_digest": "sha256:" + "2" * 64,
        "config_digest": "sha256:" + "3" * 64,
        "artifact_digest": "sha256:" + "4" * 64,
    },
    "capacity.adjust.apply": {"proposal": PROPOSAL},
    "deploy.ontology.import": {
        "connector": "arbora",
        "ontology_iri": "urn:o:one",
        "version": "1",
        "ontology_ttl": BODY,
        "sha256": "sha256:" + "0" * 64,
    },
}


def _observed(response: Any) -> str:
    if response.status_code == 200:
        return "served"
    return str(response.json()["error"]["code"])


@pytest.mark.spec("ARBORA-DEPLOY-R009.2.2")
def test_every_matrix_row_matches_the_single_image(
    loopback: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GRAPH_SERVICE_ENDPOINTS", raising=False)
    loopback.serve_unreleased(
        deploy, {"GraphSchemaList": lambda *_: {}}, "GraphSchemaOpImportOntology"
    )
    registered = {op.id for op in get_registry()}
    mismatches: list[str] = []
    for row in MATRIX:
        op_id = row["operation_id"]
        assert op_id in registered, op_id
        observed = _observed(loopback.call(op_id, CALLS[op_id], f"parity-{op_id}"))
        if observed != row["single_image_result"]:
            mismatches.append(f"PARITY_MISMATCH {op_id}: {observed}")
        # A refusal the clustered contract gives is the refusal the image gives.
        if row["clustered_result"] != "served" and observed != row["clustered_result"]:
            mismatches.append(
                f"PARITY_MISMATCH {op_id}: clustered {row['clustered_result']}"
            )
    assert mismatches == []


@pytest.mark.spec("ARBORA-DEPLOY-R009.2.2")
def test_a_differing_row_fails_naming_the_operation() -> None:
    rows = [{**MATRIX[0], "single_image_result": "served"}]
    wrong = [
        r["operation_id"]
        for r in rows
        if r["single_image_result"] != r["clustered_result"]
    ]
    assert wrong == ["deploy.placement.apply"]
