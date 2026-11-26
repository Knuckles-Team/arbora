"""Knowledge curation operations (ARBORA-OPS-R951)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import knowledge
from tests.api._knowledge_support import NO_OP, caller_context, record_engine
from tests.test_served_operation_api import _call, served  # noqa: F401


@pytest.mark.spec("ARBORA-OPS-R951")
async def test_inputs_propose_status_metrics_forward_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = record_engine(monkeypatch, knowledge, {"ok": True})
    context = caller_context()
    await knowledge.handle_curation_inputs(
        context, {"loop_id": "l", "epoch": 2, "force": False, "cursor": None}, NO_OP
    )
    await knowledge.handle_curation_propose(
        context, {"loop_id": "l", "epoch": 2, "proposal": {"p": 1}, "body": "b"}, NO_OP
    )
    await knowledge.handle_curation_status(context, {"proposal_id": "p1"}, NO_OP)
    await knowledge.handle_curation_metrics(context, {"from_ms": 1, "to_ms": 9}, NO_OP)
    assert [(m, o["op"], idem) for m, o, idem in sent] == [
        ("KnowledgeCuration", "inputs", False),
        ("KnowledgeCuration", "propose", True),
        ("KnowledgeCuration", "status", False),
        ("KnowledgeCuration", "metrics", False),
    ]
    assert sent[0][1] == {"op": "inputs", "loop_id": "l", "epoch": 2, "force": False}
    assert sent[1][1]["proposal"] == {"p": 1}


@pytest.mark.spec("ARBORA-OPS-R951")
def test_curation_operations_are_served_unavailable_until_the_scope_is_pinned(
    served: Any,  # noqa: F811
) -> None:
    app, _client = served
    reply = _call(app, "knowledge.curation.status", {"proposal_id": "p1"})
    assert reply.status_code == 503, reply.text
    assert reply.json()["error"]["code"] == "UNAVAILABLE"
