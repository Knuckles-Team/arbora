"""Onboarding writes the agent-library catalog into EG (ARBORA-FLEET-R041.17)."""

from __future__ import annotations

import contextlib
import json
import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

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


class _Graph:
    """An in-memory stand-in for the engine graph behind ``AddTriples``,
    ``RemoveTriples`` and ``Sparql`` (the three methods the catalog write uses)."""

    LINE = re.compile(r'^<([^>]*)> <([^>]*)> (<[^>]*>|".*") \.$')

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lines: set[str] = set()
        self.add_calls: list[int] = []
        self.removed: list[str] = []
        monkeypatch.setattr("medulla.generated.graph.send_add_triples", self.add)
        monkeypatch.setattr("medulla.generated.graph.send_remove_triples", self.remove)
        monkeypatch.setattr("medulla.generated.reasoning.send_sparql", self.sparql)
        monkeypatch.setattr(
            "arbora.deployment.semantic_provisioning.bind_session_graph",
            lambda _graph: contextlib.nullcontext(),
        )

    async def add(self, _client: Any, params: dict[str, Any], _graph: str) -> None:
        lines = params["ntriples"].splitlines()
        self.add_calls.append(len(lines))
        self.lines |= set(lines)

    async def remove(self, _client: Any, params: dict[str, Any], _graph: str) -> int:
        lines = set(params["ntriples"].splitlines())
        self.removed += sorted(lines)
        self.lines -= lines
        return len(lines)

    def triples(self) -> list[tuple[str, str, str]]:
        return [m.groups() for m in map(self.LINE.match, sorted(self.lines)) if m]  # type: ignore[misc]

    @staticmethod
    def _term(raw: str) -> dict[str, str]:
        if raw.startswith("<"):
            return {"kind": "iri", "value": raw[1:-1]}
        return {"kind": "literal", "value": raw[1:-1]}

    def _receipt_rows(self, tenant: str) -> list[list[dict[str, str]]]:
        by_node: dict[str, dict[str, str]] = {}
        for s, p, o in self.triples():
            by_node.setdefault(s, {})[p.rsplit("delta", 1)[-1]] = o[1:-1]
        keys = ("Tenant", "Generation", "Sequence", "Item", "Op", "Digest")
        return [
            [self._term(f'"{n[k]}"') for k in keys]
            for n in by_node.values()
            if n.get("Tenant") == tenant
        ]

    def _node_rows(self, subjects: set[str]) -> list[list[dict[str, str]]]:
        triples = self.triples()
        owned = {
            s for s, p, o in triples if p.endswith("assignedTo") and o[1:-1] in subjects
        }
        return [
            [self._term(f"<{s}>"), self._term(f"<{p}>"), self._term(o)]
            for s, p, o in triples
            if s in subjects | owned
        ]

    async def sparql(self, _client: Any, params: dict[str, Any], _graph: str) -> Any:
        query = params["query"]
        if "CatalogDeltaReceipt" in query:
            tenant = re.search(r'\?tenant = "([^"]*)"', query).group(1)  # type: ignore[union-attr]
            return SimpleNamespace(payload={"rows": self._receipt_rows(tenant)})
        listed = query.split("{", 2)[2].split("}")[0]
        subjects = set(re.findall(r"<([^>]*)>", listed))
        return SimpleNamespace(payload={"rows": self._node_rows(subjects)})


def _fleet() -> SimpleNamespace:
    return SimpleNamespace(
        session=SimpleNamespace(
            graph="tenant__homelab", actor=SimpleNamespace(actor_id="svc:arbora")
        ),
        tenant_client=object(),
        surfaces={},
        _tenant=lambda: "acme",
    )


def _rich_pack(
    *, owner: str | None, tools: tuple[str, ...] = ("get_incidents",)
) -> SimpleNamespace:
    declaration: dict[str, Any] = {
        "sources": [
            {
                "source_id": "incidents",
                "record_type": "Incident",
                "key_field": "number",
                "tools": [tools[0]],
            }
        ],
        "schema_mappings": {
            "Incident": {
                "ontology_class": "Incident",
                "fields": {"number": {"property": "urn:p#number"}, "note": {}},
            }
        },
    }
    if owner:
        declaration["owner"] = owner
    entries = [
        _entry("tool", name, f"tool://c/{name}", {"description": name})
        for name in tools
    ]
    entries.append(
        _entry("resource", "resources", "connector://resources", declaration)
    )
    return SimpleNamespace(archive=SimpleNamespace(entries=entries, data=b""))


async def _write(graph: _Graph, pack: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from arbora.fleet import onboarding

    monkeypatch.setattr(
        "arbora.deployment.semantic_provisioning.catalog_content_digest",
        lambda _pack: "e" * 64,
    )
    endpoint = onboarding.FleetEndpoint(name="c", url="http://c/mcp")
    await onboarding.FleetOnboarding.write_catalog(_fleet(), endpoint, pack)  # type: ignore[arg-type]


@pytest.mark.spec("ARBORA-FLEET-R041.17")
async def test_onboarding_writes_the_catalog_and_never_fails_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.fleet import onboarding

    graph = _Graph(monkeypatch)
    fleet = _fleet()
    monkeypatch.setattr(
        "arbora.deployment.semantic_provisioning.catalog_content_digest",
        lambda _pack: "e" * 64,
    )
    endpoint = onboarding.FleetEndpoint(name="servicenow-mcp", url="http://sn/mcp")
    await onboarding.FleetOnboarding.write_catalog(fleet, endpoint, _pack())  # type: ignore[arg-type]
    assert f"<{KG}systemPromptFor>" in "\n".join(graph.lines)
    assert fleet.surfaces["servicenow-mcp"]["catalog_triples"] > 10

    async def refused(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("ACCESS_DENIED")

    monkeypatch.setattr(catalog_graph, "write_catalog", refused)
    await onboarding.FleetOnboarding.write_catalog(fleet, endpoint, _pack())  # type: ignore[arg-type]
    assert "ACCESS_DENIED" in fleet.surfaces["servicenow-mcp"]["catalog_error"]


@pytest.mark.spec("ARBORA-FLEET-R041.17.1")
async def test_onboarding_writes_owner_version_hops_and_schema_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(monkeypatch)
    await _write(graph, _rich_pack(owner=None), monkeypatch)
    triples = set(graph.triples())
    tool = asset_iri("c", "tool", "get_incidents")
    field = asset_iri("c", "field", "incidents.note")
    resource = asset_iri("c", "resource", "incidents")
    flat = {(s, p, o) for s, p, o in triples}

    def has(s: str, p: str, o: str) -> bool:
        return (s, p, o) in flat

    # Owner missing: the importing principal holds the assignment, flagged.
    steward = tool.replace("urn:arbora:asset:", "urn:arbora:steward:", 1)
    assert has(steward, f"{CAT}assignedTo", f"<{tool}>")
    assert has(steward, f"{CAT}principal", "<urn:arbora:principal:svc%3Aarbora>")
    assert has(tool, f"{CAT}flag", '"owner_missing"')
    assert has(tool, f"{CP}packVersion", '"' + "e" * 64 + '"')
    # One lineage edge plus the direct edge per hop.
    assert has(
        f"urn:arbora:lineage:operation:{quote(tool, safe='')}:to:{quote(resource, safe='')}",
        f"{CAT}hop",
        '"operation"',
    )
    assert has(tool, f"{CAT}lineageTo", f"<{resource}>")
    assert has(resource, f"{CAT}lineageTo", f"<{field}>")
    # Schema, key and field nodes; an unmapped field is flagged.
    assert has(asset_iri("c", "schema", "incidents"), RDF_TYPE, f"<{CAT}SchemaAsset>")
    assert has(asset_iri("c", "key", "incidents.number"), f"{CAT}keyKind", '"primary"')
    assert has(field, f"{CAT}flag", '"field_unmapped"')
    assert not has(
        asset_iri("c", "field", "incidents.number"), f"{CAT}flag", '"field_unmapped"'
    )

    owned = _Graph(monkeypatch)
    await _write(owned, _rich_pack(owner="team:itsm"), monkeypatch)
    assert has_flag(owned, "owner_missing") is False
    assert any("<urn:arbora:principal:team%3Aitsm>" in line for line in owned.lines)


def has_flag(graph: _Graph, flag: str) -> bool:
    return any(f'"{flag}"' in line for line in graph.lines)


@pytest.mark.spec("ARBORA-FLEET-R041.17.1")
async def test_departed_nodes_are_removed_and_batches_are_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(monkeypatch)
    monkeypatch.setattr(catalog_graph, "batch_max_triples", lambda: 7)
    await _write(graph, _rich_pack(owner=None, tools=("a", "b")), monkeypatch)
    assert len(graph.add_calls) > 1 and max(graph.add_calls) <= 7
    gone = asset_iri("c", "tool", "b")
    assert any(gone in line for line in graph.lines)
    await _write(graph, _rich_pack(owner=None, tools=("a",)), monkeypatch)
    assert not any(line.startswith(f"<{gone}>") for line in graph.lines)
    assert any(line.startswith(f"<{gone}>") for line in graph.removed)


@pytest.mark.spec("ARBORA-FLEET-R022")
async def test_write_catalog_records_one_receipt_per_change_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.fleet.catalog_replay import read_receipts, replay_deltas

    graph = _Graph(monkeypatch)
    await _write(graph, _rich_pack(owner=None), monkeypatch)
    receipts = await read_receipts(object(), "g", "acme")
    assert [r.sequence for r in receipts] == list(range(1, len(receipts) + 1))
    assert {r.op for r in receipts} == {"upsert"}
    first = len(receipts)
    await _write(graph, _rich_pack(owner=None), monkeypatch)
    assert (
        len(await read_receipts(object(), "g", "acme")) == first
    )  # no change, no receipt
    await _write(graph, _rich_pack(owner=None, tools=("other",)), monkeypatch)
    later = await read_receipts(object(), "g", "acme")
    assert any(r.op == "remove" for r in later)
    state: dict[str, str] = {}
    replay_deltas(later, state=state, applied=set())
    assert asset_iri("c", "tool", "other") in state
    assert asset_iri("c", "tool", "get_incidents") not in state
