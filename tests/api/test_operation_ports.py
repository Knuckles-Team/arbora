"""Operation handlers reach their subsystem through ports bound in ``context.services``.

ARBORA-HOST-R038.1: the registry is offline declaration assembly and needs no port; each
handler that used to import a subsystem reads one port of the composed ``OperationPorts``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.fleet.acceptance_ports import ContextAcceptancePorts
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import apps
from arbora.api.ops.ports import (
    PORTS_BINDING,
    OperationPorts,
    port_if_composed,
    port_of,
)
from arbora.api.ops.registry_factory import get_registry

pytestmark = pytest.mark.spec("ARBORA-HOST-R038.1")

PARAMS = {"dry_run": False, "run_id": "run:1", "approve": True}


class FakeApps:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def plan(self, tenant: str, params: Any) -> dict[str, Any]:
        self.calls.append(("plan", tenant))
        return {"plan": {}}

    async def admit(self, context: Any, params: Any, *, fence: str) -> dict[str, Any]:
        self.calls.append(("admit", fence))
        return {"state": "admitted"}

    async def retire(
        self, context: Any, run_id: str, *, approve: bool
    ) -> dict[str, Any]:
        self.calls.append(("retire", run_id))
        return {"state": "retired"}


class FakeAcceptance:
    def configured(self) -> tuple[str, ...]:
        return ("alpha",)

    async def digest_matches(self, context: Any, connector: str) -> bool | None:
        return connector == "alpha"


def _context(ports: OperationPorts) -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t1"),
        idempotency_key="k1",
        services={PORTS_BINDING: ports},
    )


def test_registry_builds_from_fake_ports_and_serves_each_op_family() -> None:
    registry = get_registry()
    assert registry.get("apps.admit") is not None
    fake = FakeApps()
    context = _context(
        OperationPorts(apps=fake, fleet_acceptance=FakeAcceptance(), run_identity=None)
    )
    served = asyncio.run(
        apps.handle_apps_admit(context, PARAMS, registry["apps.admit"])
    )
    retired = asyncio.run(
        apps.handle_apps_retire_connector(
            context, PARAMS, registry["apps.retire_connector"]
        )
    )
    dry = asyncio.run(
        apps.handle_apps_admit(
            context, {**PARAMS, "dry_run": True}, registry["apps.admit"]
        )
    )
    assert served == {"value": {"state": "admitted"}}
    assert retired == {"value": {"state": "retired"}}
    assert dry == {"value": {"plan": {}}}
    assert [name for name, _ in fake.calls] == ["admit", "retire", "plan"]
    acceptance = ContextAcceptancePorts(
        context, pack_status=None, pack_op=None, probes={}
    )
    assert asyncio.run(acceptance.configured()) == ("alpha",)
    assert asyncio.run(acceptance.digest_matches("alpha")) is True


def test_a_port_the_host_did_not_compose_is_refused_not_defaulted() -> None:
    context = _context(OperationPorts.unbound())
    with pytest.raises(OperationRefused) as refused:
        port_of(context, "apps")
    assert refused.value.code == "UNAVAILABLE"
    assert port_if_composed(context, "run_identity") is None
    acceptance = ContextAcceptancePorts(
        context, pack_status=None, pack_op=None, probes={}
    )
    assert asyncio.run(acceptance.digest_matches("alpha")) is None
