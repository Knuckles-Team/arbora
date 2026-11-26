"""Process refusals map to stable codes (ARBORA-OPS-R898.6)."""

from __future__ import annotations

import pytest

from arbora.api.engine_errors import ENGINE_ERRORS
from arbora.api.errors import _ARBORA_STATUS, ArboraErrorCode


@pytest.mark.spec("ARBORA-OPS-R898.6")
def test_refusals_map_to_stable_codes() -> None:
    assert ENGINE_ERRORS["WORKFLOW_UNKNOWN"][0] == 404
    for code in ("PROCESS_STEP_UNBOUND", "PROCESS_PLAN_LOOP", "BRANCH_NO_ARM"):
        assert ENGINE_ERRORS[code][0] == 422
    assert _ARBORA_STATUS[ArboraErrorCode.APPROVAL_NOT_PENDING] == 409
    assert _ARBORA_STATUS[ArboraErrorCode.APPROVER_NOT_IN_ROLE] == 403
    assert _ARBORA_STATUS[ArboraErrorCode.PROCESS_PROMPT_UNRESOLVED] == 422
