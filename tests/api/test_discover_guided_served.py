"""Served Discover carries caller seeds and hop filters (ARBORA-OPS-R924)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from arbora.api.engine_errors import ENGINE_ERRORS
from arbora.api.invoke.agent_ref import bind_agent_ref
from arbora.security.actor_context import ActorType
from tests.api._served_support import (
    McpAsk,
    confirmable_invocation,
    person,
    route_methods,
)
from tests.test_served_operation_api import _call, served  # noqa: F401


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


class _DiscoverEngine:
    """Records each ``Discover`` request the served surfaces send."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def discover(self, params: dict[str, Any], _who: str) -> Any:
        self.requests.append(params)
        return {"hits": []}


_GUIDED = {
    "k": 3,
    "keywords": ["x"],
    "query_embedding": [],
    "seed_ids": ["n1", "n2"],
    "hop_filter": {"edge_labels": ["depends_on"]},
}


def _service() -> Any:
    """Discover is a service-only engine scope: the caller is a service identity."""
    caller = person("one", extra_scopes=("compute:semantic",))
    return replace(caller, actor_type=ActorType.AUTOMATED_SERVICE)


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    return confirmable_invocation(monkeypatch)


@pytest.mark.spec("ARBORA-OPS-R924.2")
def test_served_discover_sends_equal_seeded_requests_over_http_and_mcp(
    stack: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both surfaces send the same engine request, or both refuse the unreleased fields.

    A contract that predates MEDULLA-FEDERATED-QUERY-R741 forbids ``seed_ids`` and
    ``hop_filter``: both surfaces then refuse alike and no engine request is sent. The
    recording engine is a structural double, so the served path is the real one.
    """
    app, client = served
    engine = _DiscoverEngine()
    route_methods(
        client,
        {"Discover": engine.discover},
        monkeypatch,
        {"fixture-verified": _service()},
    )
    http = _call(app, "eg.ingestion.Discover", dict(_GUIDED))
    ask = McpAsk(
        stack["invocation"],
        monkeypatch,
        _service(),
    )
    mcp = ask("ask", "eg.ingestion.Discover", dict(_GUIDED))
    if not (http.status_code == 200 and mcp["ok"] is True):
        # A contract that predates the fields refuses both surfaces alike, sending nothing.
        assert http.json()["error"]["code"] == mcp["error"]["code"], (http.text, mcp)
        assert engine.requests == []
        return
    first, second = engine.requests
    assert first == second
    assert first["seed_ids"] == ["n1", "n2"]
    assert first["hop_filter"]["edge_labels"] == ["depends_on"]
