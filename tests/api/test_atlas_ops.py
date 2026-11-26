"""Source sync run reads wait on the engine (ARBORA-OPS-R922.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import atlas
from tests.api._knowledge_support import NO_OP, caller_context


@pytest.mark.spec("ARBORA-OPS-R922.3")
async def test_runs_list_refuses_unavailable_until_the_engine_lists_runs() -> None:
    (op,) = atlas.operations()
    assert op.id == "ingest.sources.runs.list" and op.scopes == frozenset(
        {"ingest:read"}
    )
    assert atlas.RunsListParams(state="failed", connection_id="c").limit == 25
    with pytest.raises(ValidationError):
        atlas.RunsListParams(state="stuck")
    with pytest.raises(OperationRefused) as refused:
        await atlas.handle_unserved(caller_context(), {}, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
