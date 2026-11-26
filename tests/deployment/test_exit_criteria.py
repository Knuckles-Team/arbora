"""Tests for the exit-criteria matrix audit mechanism."""

from __future__ import annotations

import pytest

from arbora.deployment.exit_criteria import audit_exit_criteria
from arbora.deployment.release_candidate import ExitCriterionRow


@pytest.mark.spec("ARBORA-RELEASE-R003.1")
def test_audit_flags_a_row_satisfied_when_its_test_is_collected() -> None:
    matrix = [
        ExitCriterionRow(
            obligation_id="write-back-receipt",
            description="one write-back receipt",
            test_reference="tests/test_write_back.py::test_receipt_recorded",
        ),
    ]
    audit = audit_exit_criteria(
        matrix, collected_test_ids=["tests/test_write_back.py::test_receipt_recorded"]
    )
    assert audit.satisfied == ("write-back-receipt",)
    assert audit.missing == ()
    assert audit.all_satisfied is True


@pytest.mark.spec("ARBORA-RELEASE-R003.1")
def test_audit_flags_a_row_missing_when_its_test_is_not_collected() -> None:
    matrix = [
        ExitCriterionRow(
            obligation_id="typed-abstention",
            description="typed-abstention response to an agent-capability question",
            test_reference="tests/test_abstention.py::test_typed_abstention",
        ),
    ]
    audit = audit_exit_criteria(matrix, collected_test_ids=[])
    assert audit.satisfied == ()
    assert audit.missing == ("typed-abstention",)
    assert audit.all_satisfied is False


@pytest.mark.spec("ARBORA-RELEASE-R003.1")
def test_audit_never_marks_an_unregistered_obligation_as_a_silent_pass() -> None:
    audit = audit_exit_criteria(matrix=[], collected_test_ids=["anything::at_all"])
    assert audit.satisfied == ()
    assert audit.missing == ()
    assert (
        audit.all_satisfied is True
    )  # vacuously true over zero rows, never fabricated


@pytest.mark.spec("ARBORA-RELEASE-R003.2")
def test_shipped_matrix_closes_across_the_seven_obligations() -> None:
    import json

    from arbora.deployment.release_candidate import (
        default_matrix_path,
        read_exit_criteria_matrix,
    )

    raw = json.loads(default_matrix_path().read_text(encoding="utf-8"))
    matrix = read_exit_criteria_matrix(raw["rows"])
    assert [row.obligation_id for row in matrix.rows] == [
        "ingestion_receipts",
        "query_proofs",
        "orchestration_graph",
        "connector_certification",
        "write_back_receipt",
        "login_probe",
        "typed_abstention",
    ]
    # A row whose test does not exist fails the audit: nothing is a silent pass.
    assert audit_exit_criteria(matrix.rows, []).missing == tuple(
        row.obligation_id for row in matrix.rows
    )
