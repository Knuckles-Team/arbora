"""The tenant model route of a role run (ARBORA-OPS-R919)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from arbora.api import model_route
from arbora.api.invoke.pipeline import OperationRefused
from tests.api._engine_double import EngineDouble, context

_LOCAL = {"endpoint": "http://models.tenant.example/v1"}
_POLICY = {
    "policy_id": "policy:esc-1",
    "roles": ["role:one"],
    "reasons": ["long_context", "local_unavailable"],
}


def _engine(monkeypatch: pytest.MonkeyPatch, record: dict[str, Any] | None) -> None:
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "KvGet",
        lambda p: (
            {"value": json.dumps(record).encode(), "version": 1}
            if record and p["namespace"] == "model.route"
            else None
        ),
    )
    engine.install(model_route)


class Probe:
    def __init__(self, up: bool) -> None:
        self.up, self.seen = up, []

    async def __call__(self, endpoint: str) -> bool:
        self.seen.append(endpoint)
        return self.up


@pytest.mark.spec("ARBORA-OPS-R919.1")
async def test_local_route_default_and_unreachable_refuses_without_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, None)
    # A tenant with no route keeps the deployment default.
    assert (
        await model_route.resolve_route(context(), "role:one", probe=Probe(True))
        is None
    )
    _engine(monkeypatch, {"local": _LOCAL})
    up = Probe(True)
    route = await model_route.resolve_route(context(), "role:one", probe=up)
    assert route == {"route": "local", "policy_id": None, "reason": None}
    assert up.seen == [_LOCAL["endpoint"]]
    with pytest.raises(OperationRefused) as down:
        await model_route.resolve_route(context(), "role:one", probe=Probe(False))
    assert down.value.code == "LOCAL_MODEL_UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R919.2")
async def test_escalation_requires_policy_and_is_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine(monkeypatch, {"local": _LOCAL})
    with pytest.raises(OperationRefused) as none:
        await model_route.resolve_route(
            context(), "role:one", {"reason": "long_context"}, probe=Probe(True)
        )
    assert none.value.code == "ESCALATION_NOT_ALLOWED"
    _engine(monkeypatch, {"local": _LOCAL, "escalation": _POLICY})
    route = await model_route.resolve_route(
        context(), "role:one", {"reason": "long_context"}, probe=Probe(True)
    )
    assert route == {
        "route": "hosted",
        "policy_id": "policy:esc-1",
        "reason": "long_context",
    }
    for role, reason in (("role:two", "long_context"), ("role:one", "because")):
        with pytest.raises(OperationRefused) as other:
            await model_route.resolve_route(
                context(), role, {"reason": reason}, probe=Probe(True)
            )
        assert other.value.code == "ESCALATION_NOT_ALLOWED"
    # An unreachable local endpoint escalates only under a policy that names it.
    fallback = await model_route.resolve_route(
        context(), "role:one", probe=Probe(False)
    )
    assert fallback is not None and fallback["reason"] == "local_unavailable"
