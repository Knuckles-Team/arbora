"""Connector prompts, resources and skills through the one door (ARBORA-FLEET-R040)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet import onboarding as onboarding_module
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.gateway_ops import FleetGateway
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.onboarding import (
    FleetEndpoint,
    FleetOnboarding,
    OnboardingReport,
    _onboard_all,
)
from arbora.fleet.session_loads import SessionLoads
from arbora.fleet.surfaces import (
    NOT_SERVED,
    item_surface,
    pack_surface_counts,
    pack_surface_report,
    surface_report,
)

FULL = "full-mcp"
BARE = "bare-mcp"
SKILL_URI = "skill://deploy/SKILL.md"
ONTOLOGY_URI = "ontology://full/core.ttl"
SHAPES_URI = "shapes://full/core.shacl"


def _items() -> tuple[CatalogItem, ...]:
    return (
        CatalogItem(f"fleet:tool:{FULL}/run", "tool", "run", server=FULL),
        CatalogItem(f"fleet:prompt:{FULL}/triage", "prompt", "triage", server=FULL),
        CatalogItem(f"fleet:skill:{FULL}/deploy", "skill", "deploy", server=FULL),
        CatalogItem(
            f"fleet:resource:{FULL}/{ONTOLOGY_URI}",
            "resource",
            ONTOLOGY_URI,
            server=FULL,
        ),
        CatalogItem(
            f"fleet:resource:{FULL}/{SHAPES_URI}", "resource", SHAPES_URI, server=FULL
        ),
        CatalogItem(f"fleet:tool:{BARE}/ping", "tool", "ping", server=BARE),
    )


class _Mux:
    """A fake multiplexer recording the governed child requests."""

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str, dict[str, str]]] = []
        self.reads: list[tuple[str, str]] = []

    async def get_server_prompt(
        self, *, server_name: str, name: str, arguments: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        self.prompts.append((server_name, name, arguments))
        return {"name": name, "description": "", "messages": []}

    async def read_server_resource(
        self, *, server_name: str, uri: str, timeout: float
    ) -> dict[str, str]:
        self.reads.append((server_name, uri))
        return {"uri": uri, "text": "body", "mimeType": "text/plain"}


def _caller(*scopes: str) -> Any:
    actor = SimpleNamespace(authenticated=True, actor_id="p", tenant_id="t")
    session = SimpleNamespace(actor=actor)
    return SimpleNamespace(
        effective_scopes=frozenset(scopes), principal="p", tenant="t", session=session
    )


def _gateway(*, hidden: frozenset[str] = frozenset()) -> tuple[FleetGateway, _Mux]:
    async def source() -> tuple[CatalogItem, ...]:
        return _items()

    async def visible(item: CatalogItem, _caller: Any) -> bool:
        return item.id not in hidden

    async def mount(_item: Any, _forwarder: Any) -> None:
        return None

    async def notify(_session: str) -> bool:
        return True

    async def invoke(*_args: Any) -> None:
        return None

    ops = MultiplexerOps(
        catalog=FleetCatalog([source], visible),
        sessions=SessionLoads(),
        loadable=visible,
        callable_item=visible,
        mount=mount,
        notify=notify,
        invoke=invoke,
        health=lambda: {},
    )
    mux = _Mux()

    async def unused(*_args: Any) -> Any:
        raise AssertionError("no tool call in a surface relay")

    gateway = FleetGateway(
        tool_for=unused,
        policy_check=unused,
        delegated_call=unused,
        catalog_ops=ops,
        surface_mux=mux,
    )
    return gateway, mux


def _patch_delegation(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib

    @contextlib.contextmanager
    def passthrough(_caller: Any) -> Any:
        yield

    monkeypatch.setattr("arbora.fleet.gateway_ops._verified_delegation", passthrough)


# spec: ARBORA-FLEET-R040
def test_surface_classification_keeps_ontology_and_shapes_apart() -> None:
    assert item_surface("resource", ONTOLOGY_URI) == "ontology"
    assert item_surface("resource", SHAPES_URI) == "ontology"
    assert item_surface("resource", "manifest://full") == "resources"
    assert item_surface("skill", "deploy") == "skills"
    assert item_surface("mcp_prompt", "triage") == "prompts"
    assert item_surface("mcp_server", "x") is None


# spec: ARBORA-FLEET-R040
def test_report_says_not_served_instead_of_a_silent_zero() -> None:
    report = surface_report(
        {"tools": 2, "skills": 0, "prompts": 0, "resources": 1, "ontology": 0}
    )
    assert report["surfaces"]["tools"] == {"count": 2, "status": "served"}
    assert report["surfaces"]["prompts"] == {"count": 0, "status": NOT_SERVED}
    assert report["surface_gap"] == ["prompts", "skills", "ontology"]


# spec: ARBORA-FLEET-R040
def test_pack_counts_each_surface_and_reports_gaps() -> None:
    def entry(kind: str, uri: str) -> Any:
        return SimpleNamespace(kind=kind, uri=uri, name=uri)

    pack = SimpleNamespace(
        archive=SimpleNamespace(
            entries=(
                entry("tool", "tool://run"),
                entry("prompt", "prompt://triage"),
                entry("skill", SKILL_URI),
                entry("resource", ONTOLOGY_URI),
                entry("resource", "manifest://full"),
            )
        )
    )
    assert pack_surface_counts(pack) == {
        "tools": 1,
        "skills": 1,
        "prompts": 1,
        "resources": 1,
        "ontology": 1,
    }
    assert pack_surface_report(pack)["surface_gap"] == []
    bare = SimpleNamespace(
        archive=SimpleNamespace(entries=(entry("tool", "tool://ping"),))
    )
    assert pack_surface_report(bare)["surface_gap"] == [
        "prompts",
        "skills",
        "ontology",
    ]


# spec: ARBORA-FLEET-R040
@pytest.mark.asyncio
async def test_onboarding_record_carries_surfaces_for_changed_and_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def entry(kind: str, uri: str) -> Any:
        return SimpleNamespace(kind=kind, uri=uri, name=uri)

    async def capture(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
        return SimpleNamespace(
            archive=SimpleNamespace(entries=(entry("tool", "tool://ping"),))
        )

    class Onboarding(FleetOnboarding):
        async def pack_unchanged(self, endpoint: Any, pack: Any) -> bool:
            return True

        async def renew(self, *_args: Any, **_kw: Any) -> tuple[str, ...]:
            return ()

        def register_contracts(self, *_args: Any) -> None:
            return None

    onboarding = Onboarding(engine=None, session=None, capture=capture)
    report = OnboardingReport()
    monkeypatch.setattr(onboarding_module, "_child_auth", lambda: None)
    endpoint = FleetEndpoint(BARE, "http://bare/mcp")
    await _onboard_all(onboarding, [endpoint], report)
    assert report.renewed == [BARE]
    rendered = report.as_dict()["surfaces"][BARE]
    assert rendered["surface_gap"] == ["prompts", "skills", "ontology"]
    assert rendered["surfaces"]["tools"]["count"] == 1


# spec: ARBORA-FLEET-R040
@pytest.mark.asyncio
async def test_catalog_search_reports_surfaces_per_connector() -> None:
    gateway, _ = _gateway()
    caller = _caller("mcp:discover")
    result = await gateway.list(caller, servers=(FULL, BARE), kinds=("tool",))
    assert [item["name"] for item in result["items"]] == ["ping", "run"]
    full = result["servers"][FULL]
    assert full["surfaces"]["prompts"]["count"] == 1
    assert full["surfaces"]["ontology"]["count"] == 2
    assert full["surface_gap"] == []
    bare = result["servers"][BARE]
    assert bare["surfaces"]["prompts"]["status"] == NOT_SERVED
    assert bare["surface_gap"] == ["prompts", "skills", "ontology"]


# spec: ARBORA-FLEET-R040
@pytest.mark.asyncio
async def test_prompts_and_resources_list_keep_the_fleet_name() -> None:
    gateway, _ = _gateway()
    caller = _caller("mcp:discover")
    prompts = await gateway.prompts_list(caller)
    assert [(i["server"], i["name"]) for i in prompts["items"]] == [(FULL, "triage")]
    resources = await gateway.resources_list(caller)
    uris = {item["uri"] for item in resources["items"]}
    assert uris == {SKILL_URI, ONTOLOGY_URI, SHAPES_URI}
    assert {item["server"] for item in resources["items"]} == {FULL}


# spec: ARBORA-FLEET-R040
@pytest.mark.asyncio
async def test_prompt_get_and_resource_reads_relay_to_the_named_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_delegation(monkeypatch)
    gateway, mux = _gateway()
    caller = _caller("mcp:discover", "mcp:delegate")
    await gateway.prompts_get(caller, server=FULL, name="triage", arguments={"a": "b"})
    await gateway.resources_read(caller, server=FULL, uri=SKILL_URI)
    await gateway.resources_read(caller, server=FULL, uri=ONTOLOGY_URI)
    assert mux.prompts == [(FULL, "triage", {"a": "b"})]
    assert mux.reads == [(FULL, SKILL_URI), (FULL, ONTOLOGY_URI)]


# spec: ARBORA-FLEET-R040
@pytest.mark.asyncio
async def test_relay_refuses_unadmitted_hidden_and_unscoped_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_delegation(monkeypatch)
    gateway, mux = _gateway(hidden=frozenset({f"fleet:resource:{FULL}/{SHAPES_URI}"}))
    caller = _caller("mcp:discover", "mcp:delegate")
    for call in (
        gateway.resources_read(caller, server=FULL, uri=SHAPES_URI),
        gateway.resources_read(caller, server=FULL, uri="ontology://nope"),
        gateway.resources_read(caller, server=BARE, uri=ONTOLOGY_URI),
        gateway.prompts_get(caller, server=FULL, name="missing"),
    ):
        with pytest.raises(PermissionError, match="unavailable"):
            await call
    with pytest.raises(PermissionError, match="mcp:delegate"):
        await gateway.resources_read(
            _caller("mcp:discover"), server=FULL, uri=ONTOLOGY_URI
        )
    with pytest.raises(PermissionError, match="mcp:discover"):
        await gateway.prompts_list(_caller("mcp:delegate"))
    assert mux.reads == [] and mux.prompts == []


# spec: ARBORA-FLEET-R040
def test_relay_operations_are_registered_and_audited() -> None:
    from arbora.api.ops.fleet import operations
    from arbora.api.registry import AuditClass, Effect

    ops = {op.id: op for op in operations()}
    for op_id in (
        "fleet.prompts.list",
        "fleet.prompts.get",
        "fleet.resources.list",
        "fleet.resources.read",
    ):
        assert ops[op_id].effect is Effect.READ
    assert ops["fleet.prompts.get"].audit is AuditClass.EVENT
    assert ops["fleet.resources.read"].audit is AuditClass.EVENT
    assert ops["fleet.resources.read"].scopes == frozenset({"mcp:delegate"})
