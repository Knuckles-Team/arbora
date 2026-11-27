"""ARBORA-FLEET-R083.2: the resolver's order is the port's; ``top_k`` clamps after it."""

from __future__ import annotations

import pytest

from arbora.api.mcp.resolve import Descriptor, IntentResolver

_OPS = tuple(
    Descriptor(id=f"deploy.op{n:02d}", verb="act", summary="deploy things")
    for n in range(30)
)


class _Reversed:
    configured = True

    def __init__(self) -> None:
        self.questions: list[str] = []

    def rank(self, question_id, query, candidates):
        self.questions.append(question_id)
        return sorted((c.id for c in candidates), reverse=True)


@pytest.mark.spec("ARBORA-FLEET-R083.2")
def test_top_k_clamps_after_the_port_order() -> None:
    port = _Reversed()
    resolver = IntentResolver(ranking=port)

    arranged = resolver.arrange("act", "deploy", _OPS, scope_ref=None, top_k=500)

    assert len(arranged) == 20  # the clamp of 20 applies after the order
    assert [c.descriptor.id for c in arranged] == [
        f"deploy.op{n:02d}" for n in range(29, 9, -1)
    ]
    assert port.questions == ["catalog.operation_rank"]


@pytest.mark.spec("ARBORA-FLEET-R083.2")
async def test_the_served_path_awaits_an_engine_ask() -> None:
    async def ask(question_id, query, candidates):
        return [c.id for c in candidates][::-1]

    from arbora.api.domain.fleet.ranking import MedullaRanking

    resolver = IntentResolver(ranking=MedullaRanking(ask))
    arranged, sourced = await resolver.arrange_sourced(
        "act", "deploy", _OPS[:3], scope_ref=None
    )

    assert [c.descriptor.id for c in arranged][0] == "deploy.op02"
    assert sourced.source == "medulla"
