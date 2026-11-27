"""The supply-chain impact report served over HTTP and MCP (ARBORA-FLEET-R052.1/.2)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.security import request_identity
from tests.api._served_support import McpAsk, route_methods
from tests.api._served_support import person as _person
from tests.test_served_operation_api import (  # noqa: F401
    _call,
    _value,
    served,
)

TENANT = "tenant:one"
AUTHORITATIVE = {"gitlab": "SoftwareComponent", "netbox": "Service"}


class Fleet:
    """The composed fleet gateway: which connectors the tenant has admitted."""

    def __init__(self, connected: list[str], tenant: str = TENANT) -> None:
        self.connected = connected
        self.tenant = tenant

    async def read_catalog(self, caller: Any) -> Any:
        return SimpleNamespace(
            context=SimpleNamespace(tenant_id=self.tenant),
            servers=[
                SimpleNamespace(component=SimpleNamespace(server_name=name))
                for name in self.connected
            ],
        )


class Engine:
    """A loopback engine: unconnected authoritative sources become abstentions."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.print_block = True

    def supply_chain_impact(self, body: dict[str, Any], _principal: str) -> Any:
        self.sent.append(body)
        used, abstained = [], []
        for source, cls in sorted(AUTHORITATIVE.items()):
            if source in body["connected_sources"]:
                used.append(
                    {
                        "source": source,
                        "class_served": cls,
                        "authority_premise": f"vg:authority:{source}",
                    }
                )
            else:
                abstained.append({"Unconnected": {"source": source, "class": cls}})
        impacts = (
            [
                {
                    "service": "svc:billing",
                    "items": [
                        {
                            "supplier": "supplier:acme",
                            "component_version": "lib:1.2",
                            "vulnerability": "CVE-1",
                            "impact_kind": "DirectDependency",
                            "source": "gitlab",
                        }
                    ],
                }
            ]
            if "gitlab" in body["connected_sources"]
            else []
        )
        report = {
            "service_impacts": impacts,
            "sources_used": used,
            "abstentions": abstained,
        }
        return {**report, **self._block(used)}

    def _block(self, used: list[dict[str, Any]]) -> dict[str, Any]:
        """The engine's sources-used-and-why block, when it prints one."""
        if not self.print_block:
            return {}
        return {
            "audit": [
                {"source": u["source"], "why": "owns the record", "rank": n}
                for n, u in enumerate(used, 1)
            ]
        }


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Compose the served stack with a fleet gateway bound (request before ``served``)."""
    import tests.test_served_operation_api as harness

    held: dict[str, Any] = {"fleet": Fleet(["gitlab", "netbox"])}
    compose = harness.compose_served_invocation

    def composing(**kwargs: Any) -> Any:
        kwargs["bindings"] = {**kwargs["bindings"], "fleet_gateway": held["fleet"]}
        held["invocation"] = compose(**kwargs)
        return held["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", composing)
    return held


def _reader() -> Any:
    """The verified person, holding the scope the report needs."""
    one = _person("one")
    return replace(one, roles=(*one.roles, "federation:read"))


def _grant(monkeypatch: pytest.MonkeyPatch) -> None:
    async def credential_owner(_credential: str) -> Any:
        return _reader()

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)


def _route(client: Any, monkeypatch: pytest.MonkeyPatch) -> Engine:
    engine = Engine()
    route_methods(
        client,
        {"SupplyChainImpact": engine.supply_chain_impact},
        monkeypatch,
        {"fixture-verified": _reader()},
    )
    return engine


@pytest.mark.spec("ARBORA-FLEET-R052.1")
def test_report_shape_and_method_absent_refusal(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    _grant(monkeypatch)
    # The pinned engine does not serve the method: no report, a typed refusal.
    refused = _call(app, "report.supply_chain_impact", {"class_roots": ["Service"]})
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == "UNAVAILABLE"
    assert [m for m, *_ in client.sent if m == "SupplyChainImpact"] == []

    engine = _route(client, monkeypatch)
    served_value = _value(
        _call(app, "report.supply_chain_impact", {"class_roots": ["Service"]})
    )
    assert served_value["report"]["service_impacts"][0]["service"] == "svc:billing"
    assert served_value["sources_used"] == served_value["report"]["sources_used"]
    assert [s["source"] for s in served_value["sources_used"]] == ["gitlab", "netbox"]
    assert served_value["abstentions"] == []
    assert engine.sent[-1]["class_roots"] == ["Service"]
    assert (
        _call(app, "report.supply_chain_impact", {"class_roots": []}).status_code == 400
    )
    assert (
        _call(
            app, "report.supply_chain_impact", {"class_roots": ["A"], "tenant": "x"}
        ).status_code
        == 400
    )


@pytest.mark.spec("ARBORA-FLEET-R052.2")
def test_removing_a_connector_turns_its_source_into_an_abstention(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    fleet = stack["fleet"]
    engine = _route(client, monkeypatch)
    both = _value(
        _call(app, "report.supply_chain_impact", {"class_roots": ["Service"]})
    )
    assert engine.sent[-1]["connected_sources"] == ["gitlab", "netbox"]
    assert both["abstentions"] == []
    assert both["report"]["service_impacts"]

    fleet.connected.remove("gitlab")
    fewer = _value(
        _call(app, "report.supply_chain_impact", {"class_roots": ["Service"]})
    )
    assert engine.sent[-1]["connected_sources"] == ["netbox"]
    # The engine's abstention passes through unchanged.
    assert fewer["abstentions"] == [
        {"Unconnected": {"source": "gitlab", "class": "SoftwareComponent"}}
    ]
    assert [s["source"] for s in fewer["sources_used"]] == ["netbox"]

    # A catalog snapshot of another tenant is never read.
    fleet.tenant = "tenant:two"
    other = _call(app, "report.supply_chain_impact", {"class_roots": ["Service"]})
    assert other.status_code >= 400
    assert len(engine.sent) == 2


@pytest.mark.spec("ARBORA-FLEET-R052.1")
def test_report_is_served_over_mcp(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, client = served
    _route(client, monkeypatch)
    import tests.api._served_support as support

    monkeypatch.setattr(support, "person", lambda *_a, **_k: _reader())
    ask = McpAsk(stack["invocation"], monkeypatch)
    reply = ask("ask", "report.supply_chain_impact", {"class_roots": ["Service"]})
    assert reply["ok"] is True, reply
    assert reply["result"]["report"]["service_impacts"][0]["service"] == "svc:billing"


@pytest.mark.spec("ARBORA-FLEET-R052.3")
def test_block_is_served_unaltered_and_its_absence_is_refused_on_both_surfaces(
    stack: dict[str, Any],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = _route(client, monkeypatch)
    import tests.api._served_support as support

    monkeypatch.setattr(support, "person", lambda *_a, **_k: _reader())
    ask = McpAsk(stack["invocation"], monkeypatch)
    params = {"class_roots": ["Service"]}

    rest = _value(_call(app, "report.supply_chain_impact", params))
    mcp = ask("ask", "report.supply_chain_impact", params)["result"]
    assert rest["audit"] == mcp["audit"] == rest["report"]["audit"]
    assert [row["source"] for row in rest["audit"]] == ["gitlab", "netbox"]

    # The engine omits the block: the one invoke pipeline refuses on both surfaces.
    engine.print_block = False
    refused_rest = _call(app, "report.supply_chain_impact", params)
    refused_mcp = ask("ask", "report.supply_chain_impact", params)
    assert refused_rest.status_code >= 400
    assert refused_rest.json()["error"]["code"] == "SOURCES_BLOCK_MISSING"
    assert refused_mcp["ok"] is False
    assert refused_mcp["error"]["code"] == "SOURCES_BLOCK_MISSING"


@pytest.mark.spec("ARBORA-FLEET-R052.3")
def test_only_tagged_operations_are_held_to_the_block() -> None:
    from arbora.api.ops.registry_factory import get_registry
    from arbora.api.registry.spec import SOURCE_AUTHORITY

    tagged = {op.id for op in get_registry() if SOURCE_AUTHORITY in op.tags}
    assert "report.supply_chain_impact" in tagged
    assert "federation.sources.list" not in tagged
