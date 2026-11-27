"""The interaction space models validate their fields (ARBORA-OPS-R053.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.ops.retrieval_space import (
    CorpusBoundary,
    InteractionSpace,
    QueryDirectory,
    RetrievalPlan,
)

pytestmark = pytest.mark.spec("ARBORA-OPS-R053.1")


def _directory(**over: object) -> QueryDirectory:
    base: dict[str, object] = {
        "name": "q01",
        "query": "what is x",
        "hit_refs": ("a",),
        "properties": {"ref": "the reference"},
        "commands": ("list",),
    }
    return QueryDirectory(**{**base, **over})  # type: ignore[arg-type]


def test_models_validate_fields() -> None:
    space = InteractionSpace(
        space_id="space:" + "a" * 32,
        boundary=CorpusBoundary(document_refs=("a",), top_k=1),
        directories=(_directory(),),
    )
    assert space.directories[0].toc_digests == ()
    with pytest.raises(ValidationError):
        _directory(name="dir1")
    with pytest.raises(ValidationError):
        _directory(query="")
    with pytest.raises(ValidationError):
        CorpusBoundary(document_refs=(), top_k=0)
    with pytest.raises(ValidationError):
        InteractionSpace(
            space_id="space:short",
            boundary=CorpusBoundary(document_refs=(), top_k=1),
            directories=(),
        )
    with pytest.raises(ValidationError):
        _directory(extra="field")
    with pytest.raises(ValidationError):
        space.boundary.top_k = 5  # type: ignore[misc]
    assert RetrievalPlan().top_k == 20 and RetrievalPlan().max_queries == 8
