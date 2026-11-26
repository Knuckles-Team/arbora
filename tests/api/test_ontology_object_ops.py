"""Ontology interface reads wait on the engine (ARBORA-OPS-R922.1)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ontology_objects
from tests.api._knowledge_support import NO_OP, caller_context


@pytest.mark.spec("ARBORA-OPS-R922.1")
async def test_interfaces_refuse_unavailable_until_the_engine_marks_them() -> None:
    ops = {op.id: op for op in ontology_objects.operations()}
    assert set(ops) == {"ontology.interfaces.list", "ontology.interfaces.implementers"}
    for op in ops.values():
        assert op.scopes == frozenset({"kg:read"})
    with pytest.raises(OperationRefused) as refused:
        await ontology_objects.handle_unserved(caller_context(), {}, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
