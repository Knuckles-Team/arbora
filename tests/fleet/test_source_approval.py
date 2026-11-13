"""ARBORA-FLEET-R042: connector data resources become sources by policy.

A read-only contract of an admitted connector is approved by
``source_approval_policy`` and sent to the engine ``ConnectorPack``
``approve_source`` operation; every other contract stays held with a named
reason, and the fleet catalog reports both counts per connector.
"""

from __future__ import annotations

import asyncio
import types
from dataclasses import dataclass
from typing import Any

import pytest

from arbora.access.action_policy import ActionPolicies
from arbora.fleet import access_contracts as ac
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.declarations import Declarations
from arbora.fleet.virtual_catalog import VirtualCatalog
from tests.fleet.test_fleet_access_contracts import TTL, _Entry, _Pack

CLS = "https://example.org/onto#Ticket"
CONNECTOR = "tickets-mcp"
HEAD_DIGEST = "b" * 64
_CALLER = types.SimpleNamespace(effective_scopes=frozenset({"mcp:discover"}))


@dataclass(frozen=True)
class _Contract:
    contract_iri: str
    effect: str | None = "read"
    auth_ref: str = "env://TICKETS_TOKEN"
    access_kind: str = "mcp_tool"
    operation: str = "list_tickets"
    source_id: str = "tickets"
    entity: str = "ticket"
    class_iri: str = CLS
    key_field: str = "id"
    predicates: tuple[tuple[str, str], ...] = (
        ("https://example.org/onto#title", "title"),
    )

    def virtual_mapping_fields(self) -> dict[str, object]:
        return {
            "mapping_id": self.contract_iri,
            "source_id": self.source_id,
            "entity": self.entity,
            "class_iri": self.class_iri,
            "key_field": self.key_field,
            "predicates": self.predicates,
        }


READ = _Contract("urn:ac:read")
WRITE = _Contract(
    "urn:ac:write", effect="write", operation="delete_ticket", entity="ticket_w"
)
SECRET = _Contract("urn:ac:secret", auth_ref="sk-live-secret", entity="ticket_s")
UNKNOWN_KIND = _Contract(
    "urn:ac:kind", access_kind="ftp_dump", entity="ticket_k", source_id="dump"
)


@pytest.fixture
def contracts(monkeypatch):
    """Install an SDK contract parser that yields the given contracts."""

    import scion_sdk.access_contract as sdk_contracts

    chosen: list[_Contract] = []
    monkeypatch.setattr(
        sdk_contracts, "parse_access_contracts", lambda _text: tuple(chosen)
    )
    return chosen


def _pack() -> Any:
    return _Pack(entries=(_Entry("ontology://tickets", TTL.encode()),))


class _Engine:
    """A loopback engine answering ConnectorPack status and approve_source."""

    def __init__(self, *, status_fails: bool = False) -> None:
        self.calls: list[str] = []
        self.status_fails = status_fails
        engine = self

        class _Client:
            async def _send(self, method, params, graph, *, idempotency_key=None):
                op = (params.get("op") or {}) if isinstance(params, dict) else {}
                name = op.get("op") if isinstance(op, dict) else None
                engine.calls.append(f"{method}.{name}")
                if method == "ConnectorPack" and name == "status":
                    if engine.status_fails:
                        raise ConnectionError("engine unreachable")
                    return engine.status()
                if method == "ConnectorPack" and name == "approve_source":
                    return engine.approve(op["request"])
                return {}

        self.client = _Client()

    class graph_compute:  # noqa: N801 - mirrors the engine attribute
        pass

    def bind(self) -> _Engine:
        client = self.client
        self.graph_compute = types.SimpleNamespace(  # type: ignore[misc]
            for_graph=lambda _name: types.SimpleNamespace(async_client=client)
        )
        return self

    @staticmethod
    def status() -> dict[str, Any]:
        return {
            "connector": CONNECTOR,
            "head": {
                "binding_revision": 3,
                "catalog": {
                    "authorization_scope_digest": "c" * 64,
                    "catalog_generation": 1,
                    "child_connection_generation": 1,
                    "configuration_revision": 1,
                    "snapshot_digest": "d" * 64,
                },
                "committed_at_ms": 1,
                "pack_digest": HEAD_DIGEST,
                "record_id": "rec-1",
                "server_package_version": "1.0.0",
            },
            "importer": None,
            "last_receipt": None,
            "members": {"published": 0, "retired": 0, "withdrawn": 0},
            "projection": {"projection": "none"},
            "schema_version": 1,
            "tenant_id": "t",
            "warnings": [],
        }

    @staticmethod
    def approve(request: dict[str, Any]) -> dict[str, Any]:
        contract = {c.contract_iri: c for c in (READ, WRITE, SECRET, UNKNOWN_KIND)}[
            request["contract_iri"]
        ]
        return {
            "batch_id": "batch-1",
            "committed_version": 7,
            "connector": request["connector"],
            "contract_digest": request["contract_digest"],
            "head": request["expected_head"],
            "schema_version": 1,
            "tenant_id": request["context"]["tenant_id"],
            "registration": {
                "approval": "approved",
                "class_iri": contract.class_iri,
                "connector": request["connector"],
                "contract_iri": contract.contract_iri,
                "entity": contract.entity,
                "key_field": contract.key_field,
                "operation": contract.operation,
                "resource_uri": request["resource_uri"],
                "source_id": contract.source_id,
                "source_kind": "mcp_tool",
            },
        }


def _onboard(monkeypatch, engine: _Engine, catalog: VirtualCatalog) -> Any:
    from arbora.deployment import semantic_provisioning
    from arbora.fleet.onboarding import FleetEndpoint, FleetOnboarding
    from arbora.security.session import use_session
    from tests._eg_fakes import service_session

    async def fake_import(*args, **kwargs):
        return None

    async def capture(endpoint, *, auth):
        return _pack()

    async def changed(self, endpoint, pack):
        return False

    monkeypatch.setattr(semantic_provisioning, "import_attested_pack", fake_import)
    monkeypatch.setattr(FleetOnboarding, "pack_unchanged", changed)
    session = service_session("t", "g")
    onboarding = FleetOnboarding(
        engine=engine.bind(),
        session=session,
        capture=capture,
        virtual_catalog=catalog,
        declarations=Declarations(),
        action_policies=ActionPolicies(),
    )
    with use_session(session):
        asyncio.run(
            onboarding.onboard(FleetEndpoint(CONNECTOR, "http://x/mcp"), auth=None)
        )
    return onboarding


@pytest.mark.spec("ARBORA-FLEET-R042.1")
def test_decision_round_trips() -> None:
    decision = ac.source_approval_policy(READ, connector=CONNECTOR, admitted=True)
    assert decision == ac.SourceApprovalDecision(
        connector=CONNECTOR,
        contract_iri="urn:ac:read",
        mapping_iri="urn:ac:read",
        outcome="approved",
        policy_id="arbora.fleet.source-approval.read-only-admitted.v1",
        reason=decision.reason,
    )
    held = ac.source_approval_policy(WRITE, connector=CONNECTOR, admitted=True)
    assert held.outcome == "held"
    assert held.policy_id == decision.policy_id
    # The record is a projection: it carries no receipt until the engine
    # returns one, and a policy approval alone selects no mapping.
    assert decision.receipt is None and decision.contract_digest is None


@pytest.mark.spec("ARBORA-FLEET-R042.2")
def test_read_only_admitted_contract_is_approved_and_write_contract_is_held(
    contracts,
) -> None:
    contracts.extend((READ, WRITE))
    catalog = VirtualCatalog()
    decisions: list[ac.SourceApprovalDecision] = []
    added = ac.register_access_contracts(
        _pack(),
        connector=CONNECTOR,
        catalog=catalog,
        admitted=True,
        decisions=decisions,
    )
    assert added == 2
    by_iri = {d.contract_iri: d for d in decisions}
    assert by_iri["urn:ac:read"].outcome == "approved"
    assert by_iri["urn:ac:write"].outcome == "held"
    assert "read-only" in by_iri["urn:ac:write"].reason
    # A repeated pass adds no duplicate mapping.
    again: list[ac.SourceApprovalDecision] = []
    assert (
        ac.register_access_contracts(
            _pack(),
            connector=CONNECTOR,
            catalog=catalog,
            admitted=True,
            decisions=again,
        )
        == 0
    )
    assert len(catalog.mappings) == 2
    # An unadmitted connector holds even the read-only contract.
    assert (
        ac.source_approval_policy(READ, connector=CONNECTOR, admitted=False).outcome
        == "held"
    )


@pytest.mark.spec("ARBORA-FLEET-R042.3")
def test_onboard_calls_policy_and_engine_registry_once(contracts, monkeypatch) -> None:
    contracts.extend((READ, WRITE))
    engine = _Engine()
    catalog = VirtualCatalog()
    onboarding = _onboard(monkeypatch, engine, catalog)

    assert engine.calls.count("ConnectorPack.approve_source") == 1
    decisions = {d.contract_iri: d for d in onboarding.source_decisions[CONNECTOR]}
    assert decisions["urn:ac:read"].receipt is not None
    assert decisions["urn:ac:read"].contract_digest
    assert decisions["urn:ac:write"].receipt is None
    approved = {m.mapping_id: m.approved for m in catalog.mappings}
    assert approved == {"urn:ac:read": True, "urn:ac:write": False}


@pytest.mark.spec("ARBORA-FLEET-R042.4")
def test_secret_unknown_kind_write_and_outage_hold_the_mapping(
    contracts, monkeypatch
) -> None:
    contracts.extend((SECRET, UNKNOWN_KIND, WRITE))
    engine = _Engine()
    catalog = VirtualCatalog()
    onboarding = _onboard(monkeypatch, engine, catalog)
    reasons = {d.contract_iri: d.reason for d in onboarding.source_decisions[CONNECTOR]}
    assert "credential reference" in reasons["urn:ac:secret"]
    assert "access kind" in reasons["urn:ac:kind"]
    assert "read-only" in reasons["urn:ac:write"]
    assert "ConnectorPack.approve_source" not in engine.calls
    assert not any(m.approved for m in catalog.mappings)

    # An engine outage leaves an approved contract held and pending a retry.
    contracts[:] = [READ]
    down = _Engine(status_fails=True)
    outage = VirtualCatalog()
    onboarding = _onboard(monkeypatch, down, outage)
    assert onboarding.surfaces[CONNECTOR]["sources_pending"]
    assert not any(m.approved for m in outage.mappings)
    assert outage.source_approval_report(CONNECTOR)["held"] == 1


@pytest.mark.spec("ARBORA-FLEET-R042.5")
def test_catalog_reports_approved_and_held_counts_with_reason(
    contracts, monkeypatch
) -> None:
    contracts.extend((READ, WRITE))
    catalog = VirtualCatalog()
    _onboard(monkeypatch, _Engine(), catalog)

    item = CatalogItem(
        id=f"{CONNECTOR}:tool:list_tickets",
        kind="tool",
        name="list_tickets",
        server=CONNECTOR,
    )

    async def source():
        return (item,)

    async def visible(_item, _caller):
        return True

    fleet = FleetCatalog(
        (source,), visible, source_approvals=catalog.source_approval_report
    )
    servers = asyncio.run(fleet.server_surfaces(_CALLER, (CONNECTOR,)))
    report = servers[CONNECTOR]["source_approvals"]
    assert report["approved"] == 1
    assert report["held"] == 1
    (held,) = report["held_mappings"]
    assert held["contract_iri"] == "urn:ac:write"
    assert "read-only" in held["reason"]
    assert held["policy_id"] == "arbora.fleet.source-approval.read-only-admitted.v1"
