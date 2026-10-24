"""Onboarding writes the agent-library catalog into EG (ARBORA-FLEET-R041.17)."""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet import catalog_graph
from arbora.fleet.catalog_graph import (
    CAT,
    CP,
    KG,
    RDF_TYPE,
    asset_iri,
    catalog_triples,
    connector_iri,
    task_iri,
    to_ntriples,
)


def _entry(kind: str, name: str, uri: str, body: Any) -> SimpleNamespace:
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return SimpleNamespace(
        kind=SimpleNamespace(value=kind), name=name, uri=uri, body=data
    )


DECLARATION = {
    "connector": "servicenow-mcp",
    "sources": [
        {
            "source_id": "incidents",
            "record_type": "Incident",
            "tools": ["servicenow_get_incidents"],
            "description": "ServiceNow incident records",
        }
    ],
    "schema_mappings": {"Incident": {"ontology_class": "Incident", "fields": {}}},
}


def _pack() -> SimpleNamespace:
    entries = [
        _entry(
            "tool",
            "servicenow_get_incidents",
            "tool://servicenow-mcp/servicenow_get_incidents",
            {"description": "List incidents", "annotations": {"readOnlyHint": True}},
        ),
        _entry(
            "prompt",
            "incident_responder",
            "prompt://servicenow-mcp/incident_responder",
            {
                "definition": {
                    "name": "incident_responder",
                    "description": "Triage ITSM incidents",
                    "_meta": {
                        "team.knuckles/prompt": {
                            "systemPromptFor": "Incident",
                            "useCase": "incident triage",
                        }
                    },
                }
            },
        ),
        _entry(
            "prompt",
            "create_branch_prompt",
            "prompt://servicenow-mcp/create_branch_prompt",
            {"definition": {"name": "create_branch_prompt"}},
        ),
        _entry(
            "skill",
            "servicenow-api-operations",
            "skill://servicenow-api-operations/SKILL.md",
            b"# skill",
        ),
        _entry("resource", "resources", "connector://resources", DECLARATION),
    ]
    return SimpleNamespace(archive=SimpleNamespace(entries=entries, data=b""))


@pytest.mark.spec("ARBORA-FLEET-R041.17")
def test_pack_becomes_typed_assets_candidates_and_lineage() -> None:
    triples = set(catalog_triples("servicenow-mcp", _pack()))
    conn = connector_iri("servicenow-mcp")
    tool = asset_iri("servicenow-mcp", "tool", "servicenow_get_incidents")
    prompt = asset_iri("servicenow-mcp", "prompt", "incident_responder")
    plain = asset_iri("servicenow-mcp", "prompt", "create_branch_prompt")
    resource = asset_iri("servicenow-mcp", "resource", "incidents")
    skill = asset_iri("servicenow-mcp", "skill", "servicenow-api-operations")
    assert (conn, RDF_TYPE, f"{CP}Connector") in triples
    assert (tool, RDF_TYPE, f"{CAT}ToolAsset") in triples
    assert (tool, f"{CAT}actionType", "http://schema.org/SearchAction") in triples
    assert (skill, RDF_TYPE, f"{CAT}SkillAsset") in triples
    for node in (tool, prompt, skill, resource):
        assert (node, f"{CP}ofConnector", conn) in triples
        assert any(s == node and p == f"{CP}digest" for s, p, _ in triples)
    # Candidacy comes from the declared meta only.
    assert (prompt, RDF_TYPE, f"{KG}PromptCandidate") in triples
    assert (prompt, f"{KG}systemPromptFor", task_iri("incident triage")) in triples
    assert (task_iri("Incident"), f"{CP}servesClass", f"{KG}Incident") in triples
    assert (task_iri("incident triage"), RDF_TYPE, f"{KG}Task") in triples
    assert (plain, RDF_TYPE, f"{KG}PromptCandidate") not in triples
    assert not any(s == plain and p == f"{KG}systemPromptFor" for s, p, _ in triples)
    # The data resource names its class and is downstream of the tool that reads it.
    assert (resource, RDF_TYPE, f"{CAT}DataResource") in triples
    assert (resource, f"{CP}servesClass", f"{KG}Incident") in triples
    assert (tool, f"{CAT}lineageTo", resource) in triples


@pytest.mark.spec("ARBORA-FLEET-R041.17")
def test_ntriples_escape_literals_and_dedupe() -> None:
    text = to_ntriples(
        [
            ("urn:a", "urn:p", ('say "hi"\n',)),
            ("urn:a", "urn:p", ('say "hi"\n',)),
            ("urn:a", "urn:q", "urn:b"),
        ]
    )
    assert text.splitlines() == [
        '<urn:a> <urn:p> "say \\"hi\\"\\n" .',
        "<urn:a> <urn:q> <urn:b> .",
    ]


@pytest.mark.spec("ARBORA-FLEET-R041.17")
async def test_onboarding_writes_the_catalog_and_never_fails_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.fleet import onboarding

    sent: list[tuple[Any, dict[str, Any], str]] = []

    async def add_triples(client: Any, params: dict[str, Any], graph: str) -> None:
        sent.append((client, params, graph))

    monkeypatch.setattr("epistemic_graph.generated.graph.send_add_triples", add_triples)
    monkeypatch.setattr(
        "arbora.deployment.semantic_provisioning._bind_session_graph",
        lambda _graph: contextlib.nullcontext(),
    )
    fleet = SimpleNamespace(
        session=SimpleNamespace(graph="tenant__homelab"),
        tenant_client=object(),
        surfaces={},
    )
    endpoint = onboarding.FleetEndpoint(name="servicenow-mcp", url="http://sn/mcp")
    await onboarding.FleetOnboarding.write_catalog(fleet, endpoint, _pack())  # type: ignore[arg-type]
    ((_, params, graph),) = sent
    assert graph == "tenant__homelab" and f"<{KG}systemPromptFor>" in params["ntriples"]
    assert fleet.surfaces["servicenow-mcp"]["catalog_triples"] > 10

    async def refused(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ACCESS_DENIED")

    monkeypatch.setattr(catalog_graph, "write_catalog", refused)
    await onboarding.FleetOnboarding.write_catalog(fleet, endpoint, _pack())  # type: ignore[arg-type]
    assert "ACCESS_DENIED" in fleet.surfaces["servicenow-mcp"]["catalog_error"]
