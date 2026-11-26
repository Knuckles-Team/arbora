"""Inbound tasks route through the engine ``A2aRoute`` decision (ARBORA-A2A-R023, R016.5)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from arbora.a2a.models import A2AMessage, A2ATextPart
from arbora.a2a.routing import A2AAssemblyUnavailable, OrchestratorA2ARouter

PRINCIPAL = "principal:sha256:" + hashlib.sha256(b"person:one").hexdigest()


def _message(**metadata: Any) -> A2AMessage:
    return A2AMessage(
        role="user",
        parts=[A2ATextPart(text="go")],
        message_id="m-1",
        metadata=metadata,
    )


class Engine:
    """A loopback ``Decide``: names the requested graph if known, else abstains."""

    def __init__(self, known: dict[str, dict[str, Any]]) -> None:
        self.known = known
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, body: dict[str, Any]) -> Any:
        self.sent.append(body)
        wanted = next(
            p["value"]["value"]
            for p in body["params"]
            if p["name"] == "a2a.requested_graph_id"
        )
        outcome: dict[str, Any]
        if wanted in self.known:
            outcome = {
                "scores": [{"option_id": wanted}],
                "capability": self.known[wanted],
            }
        else:
            outcome = {"abstained": "A2A_GRAPH_UNKNOWN"}
        return {
            "records": [
                {
                    "record_id": f"record:route:{wanted}",
                    "record_digest": "sha256:" + "cd" * 32,
                    "caller_principal": PRINCIPAL,
                    "tenant_id": body["tenant_id"],
                    "question": body["question"],
                    "inputs": {"params": body["params"]},
                    "outcome": outcome,
                }
            ]
        }


def _router(engine: Engine) -> OrchestratorA2ARouter:
    return OrchestratorA2ARouter(
        lambda: None,
        decide=engine,
        feature_schema={},
        tenant=lambda: "tenant:one",
        principal=lambda: "person:one",
    )


@pytest.mark.spec("ARBORA-A2A-R023")
def test_task_routes_through_a2a_route_decision() -> None:
    engine = Engine({"graph:known": {"model_profile_ref": "p", "task_class": "c"}})
    router = _router(engine)
    routed = asyncio.run(
        router.route(_message(agent_name="graph:known"), context_budget_tokens=None)
    )
    assert routed.agent_name == "graph:known"
    assert routed.decision_record_ref == "record:route:graph:known"
    assert [body["question"]["kind"] for body in engine.sent] == ["a2a_route"]
    with pytest.raises(A2AAssemblyUnavailable) as unknown:
        asyncio.run(
            router.route(_message(agent_name="graph:other"), context_budget_tokens=None)
        )
    assert unknown.value.reason == "A2A_GRAPH_UNKNOWN"
    # No agent name: the fleet default agent, and no decision call.
    engine.sent.clear()
    default = asyncio.run(router.route(_message(), context_budget_tokens=None))
    assert default.selection_mode == "default-agent" and engine.sent == []


@pytest.mark.spec("ARBORA-A2A-R023")
def test_an_unanswered_route_decision_routes_nothing() -> None:
    async def down(body: dict[str, Any]) -> Any:
        raise ConnectionError("down")

    with pytest.raises(A2AAssemblyUnavailable) as refused:
        asyncio.run(
            _router(down).route(_message(agent_name="g"), context_budget_tokens=None)
        )  # type: ignore[arg-type]
    assert refused.value.reason == "A2A_ROUTE_UNAVAILABLE"


@pytest.mark.spec("ARBORA-A2A-R016.5")
def test_route_decision_carries_model_profile_and_task_class() -> None:
    engine = Engine(
        {"g": {"model_profile_ref": "profile:one", "task_class": "task:chat"}}
    )
    routed = asyncio.run(
        _router(engine).route(_message(agent_name="g"), context_budget_tokens=4_096)
    )
    assert (routed.model_profile_ref, routed.task_class) == ("profile:one", "task:chat")
    # A record without either leaves them unset; admission then refuses (R017.5).
    bare = Engine({"g": {}})
    routed = asyncio.run(
        _router(bare).route(_message(agent_name="g"), context_budget_tokens=4_096)
    )
    assert (routed.model_profile_ref, routed.task_class) == (None, None)
