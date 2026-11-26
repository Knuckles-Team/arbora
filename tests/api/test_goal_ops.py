"""Goal operations wait on the engine (ARBORA-OPS-R912)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import goals
from arbora.api.registry.pending_scopes import load_pending_engine_scopes
from tests.api._knowledge_support import NO_OP, caller_context


@pytest.mark.spec("ARBORA-OPS-R912")
async def test_goal_ops_refuse_unavailable_until_the_engine_serves_goals() -> None:
    by_id = {op.id: op for op in goals.operations()}
    assert set(by_id) == {
        "agents.goals.run",
        "agents.goals.list",
        "agents.goals.iterations",
        "agents.goals.cancel",
    }
    # Run and cancel wait on a scope the pinned catalog lacks; the reads use the
    # engine's agent library read.
    assert by_id["agents.goals.run"].scopes == frozenset({"runs:control"})
    assert "runs:control" in load_pending_engine_scopes()
    assert by_id["agents.goals.list"].scopes == frozenset({"agent:library-read"})
    with pytest.raises(OperationRefused) as refused:
        await goals.handle_unserved(caller_context(), {}, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
