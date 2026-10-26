"""ARBORA-FLEET-R030: access contracts register as unapproved mappings."""

from __future__ import annotations

import builtins
import sys
import types
from dataclasses import dataclass

import pytest

from arbora.fleet import access_contracts as ac

vg = pytest.importorskip("agent_utilities.knowledge_graph.virtual_graph")
VirtualCatalog = vg.VirtualCatalog
TripleOntology = vg.TripleOntology

CLS = "https://example.org/onto#Ticket"


@dataclass(frozen=True)
class _Entry:
    uri: str
    body: bytes


def _Pack(entries: tuple[_Entry, ...] = ()) -> types.SimpleNamespace:  # noqa: N802
    """A real SDK 0.5 captured pack: bodies live in the archive, by section."""
    from epistemic_graph.connector_pack import (
        ConnectorPackArchiveBuilder,
        ConnectorPackEntryContent,
    )
    from epistemic_graph.generated.connector_pack import PackEntryKind

    def content(
        kind: PackEntryKind, uri: str, body: bytes
    ) -> ConnectorPackEntryContent:
        return ConnectorPackEntryContent(
            kind=kind, uri=uri, name=uri, media_type="text/turtle", body=body
        )

    archive = ConnectorPackArchiveBuilder.build(
        content(PackEntryKind.MCP_SERVER, "mcp-server://t", b"{}"),
        tuple(
            content(
                PackEntryKind.ONTOLOGY
                if e.uri.startswith("ontology://")
                else PackEntryKind.SHAPES,
                e.uri,
                e.body,
            )
            for e in entries
        ),
    )
    return types.SimpleNamespace(archive=archive)


@dataclass(frozen=True)
class _Contract:
    contract_iri: str
    source_id: str = "tickets"
    entity: str = "ticket"
    class_iri: str = CLS
    key_field: str = "id"
    access_kind: str = "mcp_tool"
    operation: str = "list_tickets"
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


TTL = "@prefix ac: <https://example.org/ac#> . ac:c a ac:AccessContract ."


@pytest.fixture
def fake_sdk(monkeypatch):
    seen: list[str] = []

    def parse(text: str):
        seen.append(text)
        return (_Contract("urn:ac:1"), _Contract("urn:ac:2", entity="ticket"))

    module = types.ModuleType("agent_connector_sdk.access_contract")
    module.parse_access_contracts = parse
    monkeypatch.setitem(sys.modules, "agent_connector_sdk.access_contract", module)
    return seen


def _pack() -> types.SimpleNamespace:
    return _Pack(
        entries=(
            _Entry("ontology://tickets", TTL.encode()),
            _Entry("shapes://tickets", b"ignored"),
        )
    )


@pytest.mark.spec("ARBORA-FLEET-R030")
def test_registers_unapproved_mappings(fake_sdk) -> None:
    catalog = VirtualCatalog()
    added = ac.register_access_contracts(
        _pack(), connector="tickets-mcp", catalog=catalog
    )
    assert added == 2
    assert fake_sdk == [TTL]
    connection = catalog.connections["tickets"]
    assert connection.kind == "mcp"
    assert connection.endpoint_ref == "fleet://tickets-mcp"
    assert [m.approved for m in catalog.mappings] == [False, False]
    ontology = TripleOntology([])
    assert catalog.mapping_for(CLS, ontology) is None


@pytest.mark.asyncio
async def test_registered_adapter_refuses_until_governed_reader_is_bound(
    fake_sdk,
) -> None:
    catalog = VirtualCatalog()
    ac.register_access_contracts(_pack(), connector="tickets-mcp", catalog=catalog)

    with pytest.raises(LookupError, match="no governed live binding"):
        await catalog.adapters["tickets"].fetch(catalog.mappings[0])


def _approved_receipt(
    contract: _Contract, *, resource_uri: str = "ontology://tickets"
) -> dict[str, object]:
    return {
        "connector": "tickets-mcp",
        "contract_digest": "a" * 64,
        "tenant_id": "tenant-a",
        "registration": {
            "approval": "approved",
            "class_iri": contract.class_iri,
            "connector": "tickets-mcp",
            "contract_iri": contract.contract_iri,
            "entity": contract.entity,
            "key_field": contract.key_field,
            "operation": contract.operation,
            "resource_uri": resource_uri,
            "source_id": contract.source_id,
            "source_kind": "mcp_tool",
        },
    }


def test_engine_receipt_projects_approval_only_when_registration_matches(fake_sdk):
    contract = _Contract("urn:ac:1")
    approved = ac.source_approval_decision(
        contract,
        _approved_receipt(contract),
        connector="tickets-mcp",
        resource_uri="ontology://tickets",
    )
    assert approved.outcome == "approved"
    assert approved.mapping_iri == contract.contract_iri

    held = ac.source_approval_decision(
        contract,
        _approved_receipt(contract, resource_uri="ontology://other"),
        connector="tickets-mcp",
        resource_uri="ontology://tickets",
    )
    assert held.outcome == "held"
    assert "does not match" in held.reason


def test_approved_receipt_binds_existing_mapping_without_binding_adapter(fake_sdk):
    catalog = VirtualCatalog()
    ac.register_access_contracts(_pack(), connector="tickets-mcp", catalog=catalog)
    contract = _Contract("urn:ac:1")
    decision = ac.bind_approved_mapping(
        catalog,
        contract,
        _approved_receipt(contract),
        connector="tickets-mcp",
        resource_uri="ontology://tickets",
    )
    assert decision.outcome == "approved"
    assert catalog.mappings[0].approved
    # The projection controls selection only; direct catalog fetch remains
    # fail-closed until the governed FleetSourceExecutor is used.
    with pytest.raises(LookupError, match="no governed live binding"):
        awaitable = catalog.adapters["tickets"].fetch(catalog.mappings[0])
        import asyncio

        asyncio.run(awaitable)


def test_repeated_registration_preserves_eg_approval(fake_sdk):
    from dataclasses import replace

    catalog = VirtualCatalog()
    ac.register_access_contracts(_pack(), connector="tickets-mcp", catalog=catalog)
    catalog.mappings[0] = replace(catalog.mappings[0], approved=True)
    ac.register_access_contracts(_pack(), connector="tickets-mcp", catalog=catalog)
    assert catalog.mappings[0].approved


def test_repeat_pass_adds_no_duplicate(fake_sdk) -> None:
    catalog = VirtualCatalog()
    ac.register_access_contracts(_pack(), connector="t", catalog=catalog)
    assert ac.register_access_contracts(_pack(), connector="t", catalog=catalog) == 0
    assert len(catalog.mappings) == 2


def test_missing_sdk_module_skips(monkeypatch) -> None:
    monkeypatch.delitem(
        sys.modules, "agent_connector_sdk.access_contract", raising=False
    )
    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "agent_connector_sdk.access_contract":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    catalog = VirtualCatalog()
    assert ac.register_access_contracts(_pack(), connector="t", catalog=catalog) == 0
    assert catalog.mappings == []


def test_onboarding_registers_after_import(fake_sdk, monkeypatch) -> None:
    import asyncio

    from arbora.api.contracts.session import use_session
    from arbora.deployment import semantic_provisioning
    from arbora.fleet.onboarding import FleetEndpoint, FleetOnboarding
    from tests._eg_fakes import service_session

    async def fake_import(*args, **kwargs):
        return None

    async def capture(endpoint, *, auth):
        return _pack()

    monkeypatch.setattr(semantic_provisioning, "import_attested_pack", fake_import)
    catalog = VirtualCatalog()

    class _StatusClient:
        """Answers ConnectorPack.status with no head: always "changed"."""

        async def _send(self, _method, _params, _graph, *, idempotency_key=None):
            return {
                "connector": "tickets-mcp",
                "head": None,
                "importer": None,
                "last_receipt": None,
                "members": {"published": 0, "retired": 0, "withdrawn": 0},
                "projection": {"projection": "none"},
                "schema_version": 1,
                "tenant_id": "t",
                "warnings": [],
            }

    class _Engine:
        class graph_compute:
            @staticmethod
            def for_graph(name):
                return types.SimpleNamespace(async_client=_StatusClient())

    session = service_session("t", "g")
    onboarding = FleetOnboarding(
        engine=_Engine(),
        session=session,
        capture=capture,
        virtual_catalog=catalog,
    )
    with use_session(session):
        asyncio.run(
            onboarding.onboard(FleetEndpoint("tickets-mcp", "http://x/mcp"), auth=None)
        )
    assert len(catalog.mappings) == 2
    assert not any(m.approved for m in catalog.mappings)


AC_NS = "https://knuckles-team.github.io/agent-connector-sdk/access#"
DOMAIN_TTL = (
    "@prefix ex: <https://example.org/onto#> .\n"
    'ex:Ticket ex:comment """A ticket.\nSpans lines.""" .\n'
)


def _real_parser():
    return pytest.importorskip("agent_connector_sdk.access_contract")


def test_only_contract_bodies_reach_parser(fake_sdk) -> None:
    pack = _Pack(
        entries=(
            _Entry("ontology://domain", DOMAIN_TTL.encode()),
            _Entry("ontology://contracts", TTL.encode()),
        )
    )
    catalog = VirtualCatalog()
    assert ac.register_access_contracts(pack, connector="t", catalog=catalog) == 2
    assert fake_sdk == [TTL]


def test_malformed_contracts_file_is_skipped(monkeypatch, caplog) -> None:
    def parse(text: str):
        raise ValueError("bad turtle")

    module = types.ModuleType("agent_connector_sdk.access_contract")
    monkeypatch.setattr(module, "parse_access_contracts", parse, raising=False)
    monkeypatch.setitem(sys.modules, "agent_connector_sdk.access_contract", module)
    pack = _Pack(entries=(_Entry("ontology://broken", TTL.encode()),))
    catalog = VirtualCatalog()
    with caplog.at_level("WARNING"):
        added = ac.register_access_contracts(pack, connector="t", catalog=catalog)
    assert added == 0
    assert "ontology://broken" in caplog.text


def test_real_parser_skips_triple_quoted_domain_ontology() -> None:
    sdk = _real_parser()
    pack = _Pack(
        entries=(
            _Entry("ontology://domain", DOMAIN_TTL.encode()),
            _Entry("ontology://contracts", b"@prefix ac: <" + AC_NS.encode() + b"> ."),
        )
    )
    assert ac.parse_pack_contracts(pack) == ()
    with pytest.raises(sdk.AccessContractError):
        sdk.parse_access_contracts(DOMAIN_TTL)


@pytest.mark.spec("ARBORA-FLEET-R027.8")
@pytest.mark.parametrize(
    ("approved", "expected"), [(frozenset(), False), (frozenset({"tickets-mcp"}), True)]
)
def test_onboarding_passes_the_operator_mass_withdrawal_decision(
    fake_sdk, monkeypatch, approved, expected
) -> None:
    """Only a server the operator named may withdraw most of its pack surface."""
    import asyncio

    from arbora.api.contracts.session import use_session
    from arbora.deployment import semantic_provisioning
    from arbora.deployment.production_ops import _parser, _server_names
    from arbora.fleet.onboarding import FleetEndpoint, FleetOnboarding
    from tests._eg_fakes import service_session

    seen: list[bool] = []

    async def fake_import(*args, **kwargs):
        seen.append(kwargs["allow_mass_withdrawal"])

    async def capture(endpoint, *, auth):
        return _pack()

    monkeypatch.setattr(semantic_provisioning, "import_attested_pack", fake_import)

    class _StatusClient:
        async def _send(self, _method, _params, _graph, *, idempotency_key=None):
            return {
                "connector": "tickets-mcp",
                "head": None,
                "importer": None,
                "last_receipt": None,
                "members": {"published": 0, "retired": 0, "withdrawn": 0},
                "projection": {"projection": "none"},
                "schema_version": 1,
                "tenant_id": "t",
                "warnings": [],
            }

    class _Engine:
        class graph_compute:
            @staticmethod
            def for_graph(name):
                return types.SimpleNamespace(async_client=_StatusClient())

    session = service_session("t", "g")
    onboarding = FleetOnboarding(
        engine=_Engine(),
        session=session,
        capture=capture,
        virtual_catalog=VirtualCatalog(),
        allow_mass_withdrawal=approved,
    )
    with use_session(session):
        asyncio.run(
            onboarding.onboard(FleetEndpoint("tickets-mcp", "http://x/mcp"), auth=None)
        )
    assert seen == [expected]
    args = _parser().parse_args(
        [
            "onboard-fleet",
            "--allow-mass-withdrawal",
            "a-mcp,b-mcp",
            "--allow-mass-withdrawal",
            "c-mcp",
        ]
    )
    assert _server_names(args.allow_mass_withdrawal) == {"a-mcp", "b-mcp", "c-mcp"}
