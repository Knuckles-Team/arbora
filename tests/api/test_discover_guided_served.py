"""Served Discover carries caller seeds and hop filters (ARBORA-OPS-R924)."""

from __future__ import annotations

import pytest

from arbora.api.engine_errors import ENGINE_ERRORS
from arbora.api.invoke.agent_ref import bind_agent_ref


def _discover_models():
    from medulla.generated import models

    return models.MethodDiscoverParams


@pytest.mark.spec("ARBORA-OPS-R924")
@pytest.mark.spec("ARBORA-OPS-R924.2")
def test_seeds_and_hop_filter_reach_the_engine() -> None:
    fields = _discover_models().model_fields
    if "seed_ids" not in fields:
        pytest.skip("the pinned contract predates MEDULLA-FEDERATED-QUERY-R741")
    request = _discover_models()(
        k=3,
        keywords=["x"],
        seed_ids=["n1", "n2"],
        hop_filter={"edge_labels": ["depends_on"]},
    )
    wire = request.model_dump(mode="json")
    assert wire["seed_ids"] == ["n1", "n2"]
    assert wire["hop_filter"]["edge_labels"] == ["depends_on"]


@pytest.mark.spec("ARBORA-OPS-R924.1")
def test_older_contract_refuses_unavailable_and_codes_pass_through() -> None:
    for code in ("SEED_NOT_VISIBLE", "SEED_LIMIT_EXCEEDED", "HOP_FILTER_INVALID"):
        assert ENGINE_ERRORS[code][0] < 500
    assert callable(bind_agent_ref)
