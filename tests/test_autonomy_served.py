"""The served host composition binds the autonomy verdict gate (ARBORA-OPS-R867)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.invoke.autonomy import require_autonomy_clearance
from tests.question_kinds import declare_question_kinds
from tests.test_served_gate_composition import GateClient, _compose


class VerdictClient(GateClient):
    """The engine fake answering the autonomy verdict question."""

    def __init__(self, verdicts: dict[str, Any]) -> None:
        super().__init__({})
        self.verdicts = verdicts

    async def _send(
        self, method: str, params: Any, graph: str | None, **kw: Any
    ) -> Any:
        if method != "Decide":
            return await super()._send(method, params, graph, **kw)
        self.sent.append((method, params, graph))
        action = params["request"]["question"]["action_class"]
        answer = self.verdicts[action]
        if isinstance(answer, Exception):
            raise answer
        return {"verdict": answer}


async def _clear(services: Any, op_id: str, *, approved: bool = False) -> Any:
    return await require_autonomy_clearance(
        services.autonomy,
        op_id=op_id,
        tenant="tenant:one",
        is_read=False,
        plan_approved=approved,
    )


@pytest.mark.spec("ARBORA-OPS-R867")
def test_host_composition_runs_the_autonomy_gate_against_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = VerdictClient(
        {
            "safe.op": "Allow",
            "wire.money": "RequireConfirmation",
            "broken.op": RuntimeError("engine down"),
        }
    )
    declare_question_kinds(monkeypatch, "autonomy_verdict")
    services = _compose(client, monkeypatch).services
    assert services.autonomy is not None

    assert asyncio.run(_clear(services, "safe.op")) is None
    held = asyncio.run(_clear(services, "wire.money"))
    assert held.code == "CONFIRMATION_REQUIRED"
    assert asyncio.run(_clear(services, "wire.money", approved=True)) is None
    down = asyncio.run(_clear(services, "broken.op"))
    assert down.code == "UNAVAILABLE"
    assert down.details == {"reason": "autonomy_verdict_unavailable"}

    question = client.sent[0][1]["request"]
    assert question["tenant_id"] == "tenant:one"
    assert question["question"] == {
        "kind": "autonomy_verdict",
        "action_class": "safe.op",
    }


@pytest.mark.spec("ARBORA-OPS-R867")
def test_host_composition_omits_the_gate_until_the_contract_names_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _compose(VerdictClient({}), monkeypatch).services
    assert services.autonomy is None
