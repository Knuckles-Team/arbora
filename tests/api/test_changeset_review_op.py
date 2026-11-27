"""Changeset review calls medulla Decide and returns the record digest (ARBORA-OPS-R870)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _decide, review
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context

_OP = review.operations()[0]
_PARAMS = {"repository": "r", "base_rev": "b1", "head_rev": "h1"}


class _Reader:
    async def diff(self, **kw):
        return {"files": 1}


class _Producer:
    async def findings(self, **kw):
        return [{"class": "bug", "evidence": "line 3"}]


@pytest.mark.spec("ARBORA-OPS-R870")
async def test_op_calls_decide_and_returns_the_record_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    sealed = {"id": "rec-1", "secret": "engine-owned"}
    engine.serve("Decide", lambda p: sealed)
    engine.install(_decide)
    context = engine_context()
    context.services = {"source_reader": _Reader(), "review_producer": _Producer()}
    result = await review.handle_review_changeset(context, _PARAMS, _OP)
    assert result["value"] == {
        "record_digest": _decide.record_digest(sealed),
        "base_rev": "b1",
        "head_rev": "h1",
    }
    question = engine.sent[0][1]["request"]["question"]
    assert question["kind"] == "changeset_review" and question["head_rev"] == "h1"
    context.services = {"source_reader": _Reader()}
    with pytest.raises(OperationRefused) as absent:
        await review.handle_review_changeset(context, _PARAMS, _OP)
    assert absent.value.code == "UNAVAILABLE"
