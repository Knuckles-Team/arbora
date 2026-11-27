"""The host composes the onboarding ports at boot (ARBORA-FLEET-R002.5, R045, R058).

The test drives ``start_fleet_onboarding``, the one boot entry the served process
calls, and observes the composed onboarding, the declared worker unit and the
data-prep declarations that reached the engine.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet import onboarding as onboarding_module
from arbora.fleet.definition_admission import DefinitionAdmission

DIGEST = "a" * 64


class _Client:
    pass


class _Process:
    """A started worker process the host supervises."""

    def poll(self) -> None:
        return None


def _engine() -> Any:
    client = _Client()
    return SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(async_client=client)
        )
    )


@pytest.fixture
def booted(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> dict[str, Any]:
    path = tmp_path / "data_prep.yml"
    path.write_text(
        "declarations:\n"
        f"  - capability: redact\n    field_refs: [a]\n    policy_digest: {DIGEST}\n"
    )
    sent: list[tuple[str, dict[str, Any]]] = []

    async def send_by_id(client: Any, method: str, params: dict, graph: str, **_: Any):
        sent.append((method, params))
        return {}

    monkeypatch.setattr("medulla.generated.send_by_id", send_by_id)
    monkeypatch.setattr("arbora.pin_lag.medulla.require_methods", lambda *m: None)
    monkeypatch.setattr("arbora.deployment.data_prep.data_prep_path", lambda: path)
    monkeypatch.setenv("GRAPH_SERVICE_ENDPOINTS", "tcp://engine:7000")
    started: list[Any] = []
    monkeypatch.setattr(
        onboarding_module.FleetOnboardingService,
        "start",
        lambda self: started.append(self) or "thread",
    )
    onboarding_module.start_fleet_onboarding(
        engine=_engine(),
        session=SimpleNamespace(graph="g", tenant="t"),
        multiplexer=SimpleNamespace(),
        virtual_catalog=SimpleNamespace(),
        declarations=SimpleNamespace(),
        action_policies=SimpleNamespace(),
        served_url="",
        spawn_worker=lambda argv: _Process(),
    )
    return {"sent": sent, "service": started[0]}


@pytest.mark.spec("ARBORA-FLEET-R002.5")
def test_boot_sets_the_definition_admission(booted: dict[str, Any]) -> None:
    admission = booted["service"].onboarding.definition_admission
    assert isinstance(admission, DefinitionAdmission)
    assert admission.port is not None and admission.write is not None


@pytest.mark.spec("ARBORA-FLEET-R058")
def test_boot_declares_each_data_prep_capability_once(booted: dict[str, Any]) -> None:
    declared = [p for m, p in booted["sent"] if m == "DataPrepDeclare"]
    assert [row["capability"] for row in declared] == ["redact"]


@pytest.mark.spec("ARBORA-FLEET-R045")
def test_boot_declares_the_ingest_worker_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    from arbora.fleet.host_composition import compose_onboarding_ports

    monkeypatch.setenv("GRAPH_SERVICE_ENDPOINTS", "tcp://engine:7000, tcp://b:1")
    ports = compose_onboarding_ports(
        _engine(),
        SimpleNamespace(graph="g", tenant="t"),
        spawn=lambda argv: _Process(),
    )
    assert ports.ingest_worker is not None
    assert ports.ingest_worker.argv[-1] == "tcp://engine:7000"
    monkeypatch.delenv("GRAPH_SERVICE_ENDPOINTS")
    bare = compose_onboarding_ports(_engine(), SimpleNamespace(graph="g", tenant="t"))
    assert bare.ingest_worker is None
