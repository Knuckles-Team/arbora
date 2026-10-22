"""Harness surface admission and the typed refusal envelope (ARBORA-OPS-R068).

Wiring tests only: the served happy path is pending the engine release
carrying the harness component and improvement library queries.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import VerifiedCaller
from arbora.api.ops.harness_admission import (
    HARNESS_SURFACE_AUTHORITY,
    HarnessAdmissionDecision,
    HarnessAdmissionRequest,
    admission_request_for,
    admit_harness_surface,
    refusal_for,
)


def _request(**changes: object) -> HarnessAdmissionRequest:
    values: dict[str, object] = {
        "op_id": "harness.components.list",
        "tenant": "tenant-a",
        "principal": "caller-a",
        "principal_kind": "human",
        "effective_scopes": frozenset({"kg:read"}),
    }
    values.update(changes)
    return HarnessAdmissionRequest.model_validate(values)


def test_surface_authority_covers_exactly_the_served_harness_ops() -> None:
    assert set(HARNESS_SURFACE_AUTHORITY) == {
        "harness.components.list",
        "harness.components.get",
        "harness.lessons.retrieve",
        "harness.evolution.report",
        "harness.evolution.adopt",
    }
    assert all(
        surface.scopes and surface.service
        for surface in HARNESS_SURFACE_AUTHORITY.values()
    )


def test_admission_decision_is_closed_and_frozen() -> None:
    decision = HarnessAdmissionDecision(admitted=True, provenance="p:one")
    with pytest.raises(ValidationError):
        HarnessAdmissionDecision.model_validate(
            {"admitted": True, "provenance": "p:one", "unknown_field": True}
        )
    with pytest.raises(ValidationError):
        decision.admitted = False


def test_admission_request_refuses_an_unverified_principal_kind() -> None:
    caller = VerifiedCaller(
        principal="person:one",
        tenant="tenant:one",
        effective_scopes=frozenset({"kg:read"}),
        engine_claims={"principal": "person:one", "tenant": "tenant:one"},
        principal_kind="ghost",
        authenticated=True,
        delegated=False,
        credential_kind="session",
        policy_revision="v1",
    )
    with pytest.raises(OperationRefused) as excinfo:
        admission_request_for(caller, "harness.components.list")
    assert excinfo.value.code == "UNAVAILABLE"
    assert excinfo.value.details["reason"] == "HARNESS_SURFACE_UNKNOWN"
    assert excinfo.value.details["provenance"].endswith("principal-kind-unverified")


def test_unknown_surface_refuses_with_closed_reason() -> None:
    decision = admit_harness_surface(_request(op_id="harness.unknown"), services={})
    assert decision.admitted is False
    assert decision.refusal_reason == "HARNESS_SURFACE_UNKNOWN"
    assert decision.provenance == "harness.admission:harness.unknown:unknown-surface"
    assert decision.missing_scopes == ()


@pytest.mark.parametrize(
    ("op_id", "missing"),
    [
        ("harness.components.list", ("kg:read",)),
        ("harness.lessons.retrieve", ("kg:read",)),
        ("harness.evolution.adopt", ("decide:commit",)),
    ],
)
def test_missing_scope_refuses_and_names_the_scope(
    op_id: str, missing: tuple[str, ...]
) -> None:
    decision = admit_harness_surface(
        _request(op_id=op_id, effective_scopes=frozenset()), services={}
    )
    assert decision.admitted is False
    assert decision.refusal_reason == "HARNESS_SCOPE_REQUIRED"
    assert decision.missing_scopes == missing
    assert (
        decision.provenance == f"harness.admission:{op_id}:missing-{'-'.join(missing)}"
    )


@pytest.mark.parametrize(
    ("op_id", "service"),
    [
        ("harness.components.get", "harness_component_reader"),
        ("harness.lessons.retrieve", "harness_lesson_retriever"),
        ("harness.evolution.report", "harness_evolution_report_reader"),
        ("harness.evolution.adopt", "harness_adoption_runner"),
    ],
)
def test_uncomposed_service_refuses(op_id: str, service: str) -> None:
    decision = admit_harness_surface(
        _request(
            op_id=op_id,
            effective_scopes=frozenset(HARNESS_SURFACE_AUTHORITY[op_id].scopes),
        ),
        services={},
    )
    assert decision.admitted is False
    assert decision.refusal_reason == "HARNESS_READER_UNAVAILABLE"
    assert decision.provenance == f"harness.admission:{op_id}:{service}-uncomposed"


@pytest.mark.parametrize(
    ("op_id", "service", "scopes"),
    [
        ("harness.components.list", "harness_component_reader", {"kg:read"}),
        ("harness.evolution.adopt", "harness_adoption_runner", {"decide:commit"}),
    ],
)
def test_composed_service_with_scope_admits(
    op_id: str, service: str, scopes: frozenset[str]
) -> None:
    decision = admit_harness_surface(
        _request(op_id=op_id, effective_scopes=frozenset(scopes)),
        services={service: object()},
    )
    assert decision.admitted is True
    assert decision.refusal_reason is None
    assert decision.missing_scopes == ()
    assert decision.provenance == f"harness.admission:{op_id}:admitted"


def test_refusal_for_maps_to_the_closed_invoke_envelope() -> None:
    scoped = refusal_for(
        admit_harness_surface(_request(effective_scopes=frozenset()), services={})
    )
    assert isinstance(scoped, OperationRefused)
    assert scoped.code == "SCOPE_REQUIRED"
    assert scoped.details["missing_scopes"] == ["kg:read"]
    assert scoped.details["provenance"].startswith("harness.admission:")

    uncomposed = refusal_for(admit_harness_surface(_request(), services={}))
    assert uncomposed.code == "UNAVAILABLE"
    assert uncomposed.details["reason"] == "HARNESS_READER_UNAVAILABLE"
    assert uncomposed.details["provenance"].endswith("-uncomposed")

    unknown = refusal_for(
        admit_harness_surface(_request(op_id="harness.unknown"), services={})
    )
    assert unknown.code == "UNAVAILABLE"
    assert unknown.details["reason"] == "HARNESS_SURFACE_UNKNOWN"


def test_request_refuses_authority_from_params_shape() -> None:
    """The request carries verified authority only; it is closed and frozen."""
    with pytest.raises(ValidationError):
        _request(suppressed_scope="kg:write")
    with pytest.raises(ValidationError):
        HarnessAdmissionRequest.model_validate(
            {
                "op_id": "harness.components.list",
                "tenant": "tenant-a",
                "principal": "caller-a",
                "principal_kind": "human",
                "effective_scopes": frozenset({"kg:read"}),
                "extra": "no",
            }
        )
