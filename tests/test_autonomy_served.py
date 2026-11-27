"""The served host composition binds the typed autonomy verdict gate (ARBORA-OPS-R867)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest

from arbora.api.invoke.autonomy import require_autonomy_clearance
from tests.question_kinds import declare_question_kinds
from tests.test_served_gate_composition import GateClient, _compose

_PRINCIPAL = "person:one"


class _Pins:
    """The published task-plan feature-schema pin of every tenant."""

    def pin_for(self, tenant: str) -> dict[str, Any]:
        return {"component": "feature-schema", "tenant": tenant}


def _record(request: dict[str, Any], answer: Any) -> dict[str, Any]:
    """The sealed record that answers ``request`` with the typed autonomy ``answer``."""
    digest = hashlib.sha256(_PRINCIPAL.encode()).hexdigest()
    abstained = {"outcome": "abstained", "reasons": []}
    reading = {"verdict": answer, "clean_outcomes": 3, "code": None}
    typed = {"type": "autonomy", "value": reading}
    return {
        "caller_principal": f"principal:sha256:{digest}",
        "tenant_id": request["tenant_id"],
        "question": request["question"],
        "inputs": {"params": request["params"]},
        "record_id": "r1",
        "record_digest": "d1",
        "outcome": abstained if answer == "abstained" else {"outcome": "advisory"},
        **({} if answer == "abstained" else {"answer": typed}),
    }


class VerdictClient(GateClient):
    """The engine fake answering the typed autonomy question with a sealed record."""

    def __init__(self, verdicts: dict[str, Any]) -> None:
        super().__init__({})
        self.verdicts = verdicts

    async def _send(
        self, method: str, params: Any, graph: str | None, **kw: Any
    ) -> Any:
        if method != "Decide":
            return await super()._send(method, params, graph, **kw)
        self.sent.append((method, params, graph))
        request = params["request"]
        answer = self.verdicts[request["candidates"]["options"][0]["option_id"]]
        if isinstance(answer, Exception):
            raise answer
        return {"records": [_record(request, answer)]}


def _services(client: VerdictClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    from arbora.mcp_server import served_invocation  # noqa: F401

    return _compose(client, monkeypatch, bindings={"task_plan_schema_pins": _Pins()})


async def _clear(
    services: Any, op_id: str, *, approved: bool = False, irreversible: bool = False
) -> Any:
    return await require_autonomy_clearance(
        services.autonomy,
        op_id=op_id,
        tenant="tenant:one",
        is_read=False,
        plan_approved=approved,
        irreversible=irreversible,
    )


@pytest.mark.spec("ARBORA-OPS-R867")
def test_host_composition_runs_the_autonomy_gate_against_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = VerdictClient(
        {
            "safe.op": "allow",
            "wire.money": "require_confirmation",
            "new.op": "abstained",
            "broken.op": RuntimeError("engine down"),
        }
    )
    declare_question_kinds(monkeypatch, "autonomy_verdict")
    services = _services(client, monkeypatch).services
    assert services.autonomy is not None

    assert asyncio.run(_clear(services, "safe.op")) is None
    held = asyncio.run(_clear(services, "wire.money", irreversible=True))
    assert held.code == "CONFIRMATION_REQUIRED"
    assert (
        asyncio.run(_clear(services, "wire.money", approved=True, irreversible=True))
        is None
    )
    # An abstention earns no trust, so it waits for confirmation like a held verdict.
    assert asyncio.run(_clear(services, "new.op")).code == "CONFIRMATION_REQUIRED"
    down = asyncio.run(_clear(services, "broken.op"))
    assert down.code == "UNAVAILABLE"
    assert down.details == {"reason": "autonomy_verdict_unavailable"}

    request = client.sent[0][1]["request"]
    assert request["tenant_id"] == "tenant:one"
    assert request["question"] == {
        "question_id": "autonomy:safe.op",
        "kind": "autonomy_verdict",
        "safety": "ordinary",
    }
    assert request["params"] == [
        {
            "name": "autonomy_verdict",
            "value": {
                "type": "autonomy_verdict",
                "value": {"irreversibility": "reversible"},
            },
        }
    ]
    irreversible = client.sent[1][1]["request"]
    assert irreversible["question"]["safety"] == "irreversible"
    assert irreversible["params"][0]["value"]["value"] == {
        "irreversibility": "irreversible"
    }


@pytest.mark.spec("ARBORA-OPS-R867")
def test_host_composition_omits_the_gate_until_the_contract_names_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _services(VerdictClient({}), monkeypatch).services
    assert services.autonomy is None
