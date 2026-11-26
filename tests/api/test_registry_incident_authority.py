"""Arbora holds no local incident authority (ARBORA-OPS-R069)."""

from __future__ import annotations

import pytest

from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import EgMethod

#: The engine methods that own incident and health-anomaly state.
ENGINE_INCIDENT_METHODS = frozenset(
    {
        "IncidentRecordAnomaly",
        "IncidentCorrelate",
        "IncidentEvidence",
        "IncidentRelatedClaims",
        "IncidentSetStatus",
    }
)


@pytest.mark.spec("ARBORA-OPS-R069")
def test_no_local_incident_writer() -> None:
    registry = get_registry()
    incident_ops = [
        op
        for op in registry
        if "incident" in op.id.lower() or "anomal" in op.id.lower()
    ]
    assert incident_ops, "the engine incident methods must be served"
    for op in incident_ops:
        assert op.id.startswith("eg."), f"{op.id} is not an engine binding"
        assert isinstance(op.binding, EgMethod), f"{op.id} has a local handler"
    served = {
        op.binding.service
        for op in incident_ops
        if op.binding.service.startswith("Incident")
    }
    assert served == ENGINE_INCIDENT_METHODS
