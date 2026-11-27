"""The space is the first-seen union of the top-K refs, one directory per query (ARBORA-OPS-R053.2)."""

from __future__ import annotations

import pytest

from arbora.api.ops.retrieval_space import RetrievalPlan, build_space
from tests.api._retrieval_space_support import outcome


@pytest.mark.spec("ARBORA-OPS-R053.2")
def test_union_caps_and_directory_names() -> None:
    plan = RetrievalPlan(top_k=2, max_queries=3)
    space = build_space(
        ["first", "second"],
        [outcome("a", "b", "c"), outcome("b", "d")],
        plan,
    )
    # K caps each query: "c" is the third hit and stays outside the boundary.
    assert space.boundary.document_refs == ("a", "b", "d")
    assert [d.name for d in space.directories] == ["q01", "q02"]
    assert space.directories[0].hit_refs == ("a", "b")
    assert space.directories[1].hit_refs == ("b", "d")
    assert set(space.directories[0].properties) == {"ref", "text", "tokens"}
    again = build_space(
        ["first", "second"], [outcome("a", "b", "c"), outcome("b", "d")], plan
    )
    assert again.space_id == space.space_id
    other = build_space(["first"], [outcome("a")], plan)
    assert other.space_id != space.space_id
    # With no decision the stated fallback applies.
    assert RetrievalPlan().top_k == 20 and RetrievalPlan().ceiling == 160
