"""Versioned definitions are admitted by the onboarding pass before the import.

ARBORA-FLEET-R002.5, R002.7 and R002.8. The captured pack, the onboarding pass
and the definition generation are real; the engine is a structural double that
answers ``Decide`` for ``skill_upgrade`` and records the receipt write.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.fleet.engine_admission import EngineAdmission
from arbora.fleet.definition_admission import DefinitionAdmission
from arbora.fleet.onboarding import (
    FleetEndpoint,
    FleetOnboarding,
    OnboardingStepRefused,
)
from tests.fleet._certified_support import no_pin_lag
from tests.fleet.test_skill_upgrade_served import UpgradeEngine

ENDPOINT = FleetEndpoint("crm-mcp", "http://crm-mcp.example/mcp")


def _pack(version: str, properties: dict[str, Any]) -> Any:
    document = {
        "version": version,
        "inputSchema": {"type": "object", "properties": properties},
    }
    entry = SimpleNamespace(
        kind="tool",
        name="lookup",
        uri="tool://lookup",
        body=json.dumps(document).encode(),
    )
    return SimpleNamespace(archive=SimpleNamespace(entries=[entry], data=b""))


class _Onboarding(FleetOnboarding):
    """The real ``onboard`` with the engine-bound steps outside admission stubbed."""

    async def pack_unchanged(self, endpoint: Any, pack: Any) -> bool:
        return False

    def register_contracts(self, endpoint: Any, pack: Any) -> tuple[()]:
        return ()

    async def approve_contracts(self, *a: Any, **k: Any) -> None:
        return None

    async def write_catalog(self, *a: Any, **k: Any) -> None:
        return None

    def _record_contract(self, connector: str, pack: Any) -> None:
        return None

    def _check_contract(self, connector: str, pack: Any) -> None:
        return None

    async def _check_projection(self, connector: str) -> None:
        return None

    events: list[str]

    async def _import_pack(self, endpoint: Any, pack: Any) -> None:
        self.events.append("import")


class _Stack:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_pin_lag(monkeypatch)
        self.events: list[str] = []
        self.written: list[tuple[str, str]] = []
        self.pack = _pack("1.0.0", {"q": {"type": "string"}})
        self.engine = UpgradeEngine()

        async def write(ntriples: str, tenant: str) -> None:
            self.events.append("receipt")
            self.written.append((tenant, ntriples))

        async def capture(endpoint: Any, *, auth: Any) -> Any:
            return self.pack

        self.admission = DefinitionAdmission(
            EngineAdmission(self.engine.send, lambda _t: {}), write
        )
        self.onboarding = _Onboarding(
            engine=SimpleNamespace(
                graph_compute=SimpleNamespace(
                    for_graph=lambda graph: SimpleNamespace(async_client=object())
                )
            ),
            session=SimpleNamespace(graph="g", tenant="tenant-a"),
            declarations=SimpleNamespace(remember=lambda *a: None),
            action_policies=SimpleNamespace(),
            capture=capture,
            definition_admission=self.admission,
        )
        self.onboarding.events = self.events  # type: ignore[attr-defined]
        monkeypatch.setattr(
            "arbora.fleet.onboarding._remember_declaration", lambda *a: None
        )

    async def run(self) -> bool:
        return await self.onboarding.onboard(ENDPOINT, auth=None)


@pytest.mark.spec("ARBORA-FLEET-R002.5")
async def test_onboard_calls_admission_before_import(monkeypatch) -> None:
    stack = _Stack(monkeypatch)
    assert await stack.run() is True
    assert stack.events == ["import", "receipt"]  # admitted first, then imported

    stack.pack = _pack("2.0.0", {})  # a breaking major without a migration
    stack.events.clear()
    with pytest.raises(OnboardingStepRefused) as refused:
        await stack.run()
    assert refused.value.step == "DefinitionAdmission"
    assert refused.value.code == "SCHEMA_INCOMPATIBLE"
    assert stack.events == []  # no import ran on refusal
    assert stack.admission.generation.number == 1  # the prior generation stays


@pytest.mark.spec("ARBORA-FLEET-R002.7")
async def test_receipt_names_consumed_definition(monkeypatch) -> None:
    stack = _Stack(monkeypatch)
    await stack.run()
    stack.pack = _pack("1.1.0", {"q": {"type": "string"}, "r": {"type": "string"}})
    await stack.run()
    tenant, ntriples = stack.written[-1]
    assert tenant == "tenant-a"
    assert '"1.0.0"' in ntriples and '"1.1.0"' in ntriples  # old and new versions
    assert "DefinitionAdmissionReceipt" in ntriples
    assert '"dec-1"' in ntriples  # the engine decision id
    assert "inputSchema" not in ntriples and "properties" not in ntriples  # no body
    receipt = stack.admission.generation.definitions["tool:crm-mcp/lookup"]
    assert receipt.version == "1.1.0"


@pytest.mark.spec("ARBORA-FLEET-R002.8")
async def test_catalog_reload_admits_compatible_update(monkeypatch) -> None:
    stack = _Stack(monkeypatch)
    await stack.run()
    stack.pack = _pack("1.0.1", {"q": {"type": "string"}, "x": {"type": "string"}})
    assert await stack.run() is True
    assert stack.admission.generation.number == 2


@pytest.mark.spec("ARBORA-FLEET-R002.8")
async def test_breaking_forged_stale_refuse(monkeypatch) -> None:
    stack = _Stack(monkeypatch)
    await stack.run()
    # Breaking: a major bump that drops a field, no migration declared.
    stack.pack = _pack("2.0.0", {})
    with pytest.raises(OnboardingStepRefused) as breaking:
        await stack.run()
    assert breaking.value.code == "SCHEMA_INCOMPATIBLE"
    # Forged: the same version with a changed body is a conflict.
    stack.pack = _pack("1.0.0", {"forged": {"type": "string"}})
    with pytest.raises(OnboardingStepRefused) as forged:
        await stack.run()
    assert forged.value.code == "DEFINITION_VERSION_CONFLICT"
    # Stale: an older version never replaces the admitted one.
    stack.pack = _pack("0.9.0", {"q": {"type": "string"}})
    with pytest.raises(OnboardingStepRefused) as stale:
        await stack.run()
    assert stale.value.code == "DEFINITION_VERSION_CONFLICT"
    assert stack.admission.generation.number == 1 and stack.events == [
        "import",
        "receipt",
    ]


@pytest.mark.spec("ARBORA-FLEET-R002.5")
async def test_missing_engine_shape_refuses_unavailable(monkeypatch) -> None:
    stack = _Stack(monkeypatch)
    await stack.run()
    from arbora.pin_lag import medulla as pin_lag
    from arbora.pin_lag.unavailable import PinLagUnavailable

    def lacking() -> None:
        raise pin_lag.refusal(PinLagUnavailable("SkillUpgradeRequest"))

    from arbora.api.domain.fleet import engine_admission

    monkeypatch.setattr(
        engine_admission, "pin_lag", SimpleNamespace(guard_skill_upgrade=lacking)
    )
    stack.pack = _pack("1.1.0", {"q": {"type": "string"}})
    with pytest.raises(OnboardingStepRefused) as refused:
        await stack.run()
    assert refused.value.code == "PRODUCER_UNAVAILABLE"
    assert stack.engine.requests == []
