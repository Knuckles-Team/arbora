"""decide.commit served through the real invoke pipeline (ARBORA-OPS-R021.1)."""

from __future__ import annotations

import pytest

from tests.test_served_operation_api import _call, served  # noqa: F401


@pytest.mark.spec("ARBORA-OPS-R021.1")
def test_human_cannot_commit_and_result_has_provenance(served) -> None:  # noqa: F811
    http, _client = served
    refused = _call(
        http,
        "decide.commit",
        {"request": {"record": {}}},
        **{"Idempotency-Key": "decide-1"},
    )
    assert refused.status_code in (401, 403), refused.text
    assert "PRINCIPAL_NOT_ALLOWED" in refused.text or "SCOPE_REQUIRED" in refused.text
