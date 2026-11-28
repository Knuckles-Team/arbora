"""Typed status enums on browser, fleet, gateway, identity and security records keep their old JSON."""

from __future__ import annotations

import json
from enum import StrEnum

import pytest

from arbora.api.domain.browser_control.browser_control_api import (
    BrowserCallStatus,
    BrowserLeaseReceipt,
    BrowserLeaseStatus,
)
from arbora.browser_control.browser_control_client import (
    ControlResultMessage,
    ControlResultStatus,
)
from arbora.ecosystem.agent_manager_dashboard import ComponentHealth
from arbora.fleet.ingest_worker_unit import IngestWorkerState
from arbora.fleet.loop_worker import TickState
from arbora.fleet.scaling.service_scale_units import EvidenceStatus
from arbora.gateway.models import WidgetData, WidgetStatus
from arbora.gateway.registry_models import RegistryKindResult, RegistryKindStatus
from arbora.gateway.schemas.graph_core import HookStatus
from arbora.gateway.schemas.status import ResponseStatus
from arbora.identity.declaration_reconcile import DeclaredOutcome, DeclaredState
from arbora.mcp_server.composition import EngineProbe, EngineState
from arbora.security.operation_result import OperationStatus
from arbora.security.secret_diagnostics import ReportStatus


@pytest.mark.parametrize(
    ("enum", "texts"),
    [
        (BrowserLeaseStatus, ["active", "revoked", "expired"]),
        (
            BrowserCallStatus,
            [
                "pending_confirmation",
                "dispatched",
                "succeeded",
                "failed",
                "cancelled",
                "unknown",
                "denied",
            ],
        ),
        (ControlResultStatus, ["succeeded", "failed"]),
        (ComponentHealth, ["ok", "warning", "error", "stale"]),
        (IngestWorkerState, ["healthy", "unhealthy"]),
        (TickState, ["idle", "refused", "ran"]),
        (
            EvidenceStatus,
            [
                "accepted",
                "missing",
                "stale",
                "blocked",
            ],
        ),
        (
            WidgetStatus,
            [
                "ok",
                "error",
                "degraded",
                "unreachable",
                "unknown",
            ],
        ),
        (RegistryKindStatus, ["ok", "unavailable"]),
        (
            HookStatus,
            [
                "healthy",
                "stale",
                "not_installed",
                "integrated",
                "n/a",
            ],
        ),
        (ResponseStatus, ["success"]),
        (
            DeclaredState,
            [
                "converged",
                "planned",
                "drift",
                "degraded",
            ],
        ),
        (EngineState, ["running", "absent"]),
        (OperationStatus, ["succeeded", "failed", "redirected"]),
        (ReportStatus, ["complete", "incomplete"]),
    ],
)
def test_enum_texts_are_the_old_texts(enum: type[StrEnum], texts: list[str]) -> None:
    assert [member.value for member in enum] == texts


def test_records_dump_the_old_json() -> None:
    assert json.loads(WidgetData().model_dump_json())["status"] == "ok"
    assert (
        json.loads(WidgetData(status=WidgetStatus.DEGRADED).model_dump_json())["status"]
        == "degraded"
    )
    assert json.loads(RegistryKindResult().model_dump_json())["status"] == "ok"
    assert (
        json.loads(
            ControlResultMessage.model_validate(
                {"call_id": "c1", "status": "succeeded"}
            ).model_dump_json()
        )["status"]
        == "succeeded"
    )
    with pytest.raises(ValueError):
        ControlResultMessage.model_validate({"call_id": "c1", "status": "done"})
    assert EngineProbe(EngineState.ABSENT).state == "absent"
    assert DeclaredOutcome(DeclaredState.PLANNED, (), ()).exit_code == 0
    assert DeclaredOutcome(DeclaredState.DRIFT, (), ()).exit_code == 1
    assert BrowserLeaseReceipt.model_fields["status"].annotation is BrowserLeaseStatus
