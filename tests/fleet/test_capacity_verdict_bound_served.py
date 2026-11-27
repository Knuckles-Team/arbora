"""The served composition binds the engine verdict sender (ARBORA-CAPACITY-R751).

The host composition builds ``capacity_ask`` from its ``Decide`` sender, the tenant's
feature-schema pin and the host principal, so the controller loop can raise. The engine
is a recorded ``Decide`` answer that echoes the request; nothing sleeps.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.fleet.error_budget import ThrottleDecision, ThrottleMode
from arbora.mcp_server.served_invocation import _with_capacity
from tests.fleet._capacity_support import PARTITION, FakePort, controller, fill

PRINCIPAL = "service:arbora-host"
PIN = {
    "component_id": "capacity",
    "kind": "feature_schema",
    "definition_digest": "sha256:" + "0" * 64,
}


class Pins:
    def pin_for(self, tenant: str) -> dict[str, Any] | None:
        return PIN


class Pool:
    async def connected_client(self, context: Any, graph: str) -> Any:
        return SimpleNamespace(graph=graph)


def _engine(monkeypatch: pytest.MonkeyPatch, option: str, asked: list[Any]) -> None:
    async def send_by_id(client: Any, method: str, params: dict[str, Any], graph: Any):
        assert method == "Decide"
        request = params["request"]
        asked.append(request)
        who = hashlib.sha256(PRINCIPAL.encode()).hexdigest()
        return {
            "records": [
                {
                    "caller_principal": "principal:sha256:" + who,
                    "tenant_id": request["tenant_id"],
                    "question": request["question"],
                    "inputs": {"params": request["params"]},
                    "record_id": "record-1",
                    "record_digest": "digest-1",
                    "outcome": {
                        "outcome": "advisory",
                        "scores": [{"option_id": option}],
                    },
                }
            ]
        }

    import medulla.generated as generated

    monkeypatch.setattr(generated, "send_by_id", send_by_id)


def _bound(pins: Any) -> dict[str, Any]:
    session = lambda: SimpleNamespace(actor=SimpleNamespace(actor_id=PRINCIPAL))  # noqa: E731
    bindings, _ = _with_capacity(
        {"task_plan_schema_pins": pins}, Pool(), "graph", lambda t: object(), session
    )
    return dict(bindings)


def _seed(registry: Any) -> None:
    registry.set_mode(PARTITION, ThrottleMode.ENFORCE)
    registry.record_decision(
        ThrottleDecision(
            window_digest="seed",
            partition=PARTITION,
            config_version="v1",
            prior_limit=2,
            new_limit=2,
            mode=ThrottleMode.ENFORCE,
            reason="seed",
            sample_count=0,
            eligible_failure_count=0,
        )
    )


async def _window(built: Any, recorder: Any, clock: Any) -> ThrottleDecision:
    fill(recorder, PARTITION, ok=20)
    clock.advance(60)
    (decision,) = await built.tick()
    return decision


@pytest.mark.spec("ARBORA-CAPACITY-R751")
async def test_the_host_binds_the_engine_verdict_sender_so_the_loop_can_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Any] = []
    _engine(monkeypatch, "allow", asked)
    ask = _bound(Pins())["capacity_ask"]
    built, registry, recorder, clock = controller(FakePort(), ask=ask)
    _seed(registry)
    decision = await _window(built, recorder, clock)
    assert decision.new_limit == 4
    assert registry.status(PARTITION).current_limit == 4
    (request,) = asked
    assert request["question"]["kind"] == "capacity_proposal"


@pytest.mark.spec("ARBORA-CAPACITY-R751")
async def test_a_refusal_or_a_missing_pin_holds_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[Any] = []
    _engine(monkeypatch, "refuse:CAPACITY_HEADROOM_EXCEEDED", asked)
    for pins in (Pins(), None):
        ask = _bound(pins)["capacity_ask"]
        built, registry, recorder, clock = controller(FakePort(), ask=ask)
        _seed(registry)
        decision = await _window(built, recorder, clock)
        assert registry.status(PARTITION).current_limit == 2
        assert decision.reason.startswith("raise_held:")
