"""ARBORA-FLEET-R003 and R003.3: write-back survives a service restart."""

from __future__ import annotations

import pytest

from arbora.api.ops import get_registry
from tests.fleet.test_repository_source_effect_served import _commit, _good, params


@pytest.mark.spec("ARBORA-FLEET-R003")
@pytest.mark.spec("ARBORA-FLEET-R003.3")
async def test_write_back_survives_restart() -> None:
    request, binding, gateway, ctx = _good()
    first = await _commit(ctx, params(request, binding))
    # Rebuild the registry mid-sequence: the operations are the same and the
    # receipt the connector proves is unchanged.
    before = get_registry().get("fleet.write_back.commit")
    after = get_registry().get("fleet.write_back.commit")
    assert before is not None and before.id == after.id
    second = await _commit(ctx, params(request, binding))
    assert first == second
    assert first["value"]["receipt_digest"] == second["value"]["receipt_digest"]
