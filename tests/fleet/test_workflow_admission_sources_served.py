"""Workflow admission reads its declarations and grants from the fleet catalog.

ARBORA-FLEET-R034.8, R034.9 and R021.1. The invoke pipeline, the admission gate and
the narrowing policy are the real code. The catalog sources are
``CatalogAdmissionSources`` over a fleet gateway that serves one verified snapshot.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.bootstrap import _admission_gate_from_config
from arbora.fleet.admission_sources import CatalogAdmissionSources, tool_name
from tests.fleet._certified_support import (
    World,
    attestation,
    certification,
    h,
    no_pin_lag,
    workflow_metadata,
)

TENANT = "tenant:one"
TOOL = tool_name("crm-mcp", "read_contacts")


class Gateway:
    """The composed fleet gateway: one verified snapshot of the tenant's catalog."""

    def __init__(self, tenant: str = TENANT, digest: str | None = None) -> None:
        self.tenant = tenant
        self.digest = digest or h("crm.read:v1")

    async def read_catalog(self, caller: Any) -> Any:
        tool = SimpleNamespace(
            entry=SimpleNamespace(
                kind="tool",
                server_name="crm-mcp",
                upstream_name="read_contacts",
                definition_digest=self.digest,
            )
        )
        server = SimpleNamespace(provides=(tool,))
        return SimpleNamespace(
            context=SimpleNamespace(tenant_id=self.tenant),
            servers=(server,),
            generation=None,
        )


class Sources(CatalogAdmissionSources):
    """The production sources, remembering the grant revision the gate read."""

    revision: str | None = None

    async def grants(self, caller: Any) -> Any:
        grants = await super().grants(caller)
        self.revision = grants["revision"]
        return grants


@pytest.fixture(autouse=True)
def _pin_lag(monkeypatch: pytest.MonkeyPatch) -> None:
    no_pin_lag(monkeypatch)


def _world(gateway: Gateway) -> tuple[World, Sources]:
    sources = Sources(gateway)
    world = World(sources=sources)
    world.engine.store(attestation("atom"))
    return world, sources


def _metadata(revision: str, digest: str | None = None) -> dict[str, Any]:
    return {
        "workflow": workflow_metadata(
            tools={TOOL: digest or h("crm.read:v1")},
            grant_revision=revision,
            dependencies={"atom": ()},
            certifications=(certification("atom"),),
        )
    }


def _code(result: Any) -> str:
    return str(getattr(result, "code", ""))


@pytest.mark.spec("ARBORA-FLEET-R034.8")
def test_catalog_grants_admit_a_workflow_at_their_current_revision() -> None:
    world, sources = _world(Gateway())
    stale = world.call(_metadata("old-revision"), key="k1")
    assert _code(stale) == "WORKFLOW_NOT_ADMITTED"
    assert "run" not in world.journal
    assert sources.revision is not None

    admitted = world.call(_metadata(sources.revision), key="k2")
    assert _code(admitted) == "OK", admitted
    assert world.journal.count("run") == 1


@pytest.mark.spec("ARBORA-FLEET-R034.9")
def test_a_tool_whose_declared_digest_changed_blocks_every_call() -> None:
    world, sources = _world(Gateway(digest=h("crm.read:v2")))
    world.call(_metadata("probe"), key="k1")
    refused = world.call(_metadata(sources.revision or "", h("crm.read:v1")), key="k2")
    assert _code(refused) == "CERTIFICATION_STALE"
    assert "run" not in world.journal


@pytest.mark.spec("ARBORA-FLEET-R021.1")
def test_a_catalog_of_another_tenant_is_never_read() -> None:
    world, _sources = _world(Gateway(tenant="tenant:two"))
    refused = world.call(_metadata("any"), key="k1")
    assert _code(refused) == "WORKFLOW_NOT_ADMITTED" and "run" not in world.journal


@pytest.mark.spec("ARBORA-FLEET-R034.8")
def test_the_host_binds_the_catalog_sources_when_it_has_a_fleet_gateway() -> None:
    bound = _admission_gate_from_config(None, {"fleet_gateway": Gateway()})
    assert bound.grants is not None and bound.declarations is not None
    bare = _admission_gate_from_config(None, {})
    assert bare.grants is None and bare.declarations is None
