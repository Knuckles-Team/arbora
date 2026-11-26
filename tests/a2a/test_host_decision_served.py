"""A budgeted message admits through the host composition (ARBORA-A2A-R017.5, R017.6, R023).

``build_host_context`` builds the A2A service the host serves; ``bind_a2a_decision``
binds its ``Decide`` port to the served invocation's engine sender, the tenant's
published feature-schema pin and the host service principal, exactly as the confirm
deciders are bound. A scripted engine answers ``Decide`` with records shaped as medulla
``main`` seals them. The request goes through ``POST /a2a`` and the real router,
harness decider and admission; only the WorkItem authority is a recording double.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.mcp_server.host_context import bind_a2a_decision, build_host_context
from tests.a2a._admission_support import DIGEST, TOOLS, answer_json
from tests.a2a._service_support import HEADERS, RecordingAuthority

TENANT = "tenant:one"
HOST = "service:host"
SCHEMA = {"schema_version": 1, "body": "pin"}


def _caller() -> Any:
    from arbora.api.mcp.caller import caller_from_session
    from arbora.security.actor_context import ActorContext, ActorType, CredentialLease
    from arbora.security.request_identity import _resolve_authenticated_scopes
    from arbora.security.session import GraphSession
    from tests.test_real_boot import ROLES

    actor = ActorContext(
        actor_id="person:caller",
        actor_type=ActorType.HUMAN,
        roles=ROLES,
        tenant_id=TENANT,
        authenticated=True,
        credential_lease=CredentialLease(int(time.time()) + 600),
    )
    session = GraphSession(
        actor=actor,
        tenant=TENANT,
        scopes=_resolve_authenticated_scopes(actor),
        graph="__control__",
        audience="arbora-test",
        policy_version="revision:one",
    )
    return caller_from_session(session, credential_kind="session", request_id="r1")


class Engine:
    """The host's engine sender: ``(method, params, tenant)`` to a decoded batch."""

    def __init__(self, *, typed_route: bool = True) -> None:
        self.typed_route = typed_route
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def __call__(self, method: str, body: dict[str, Any], tenant: str) -> Any:
        self.calls.append((method, body, tenant))
        kind = body["question"]["kind"]
        record: dict[str, Any] = {
            "record_id": f"record:{kind}:1",
            "record_digest": "sha256:" + DIGEST,
            "caller_principal": "principal:sha256:"
            + hashlib.sha256(HOST.encode()).hexdigest(),
            "tenant_id": body["tenant_id"],
            "question": body["question"],
            "inputs": {"params": body["params"]},
        }
        if kind == "a2a_route":
            wanted = body["params"][0]["value"]["value"]
            record["outcome"] = {
                "outcome": "advisory",
                "calibrated": False,
                "ranked_total": 1,
                "scores": [{"option_id": wanted}],
            }
            if self.typed_route:
                record["answer"] = {
                    "type": "a2a_route",
                    "value": {
                        "graph_id": wanted,
                        "model_profile_ref": "profile:one",
                        "task_class": "task:chat",
                    },
                }
        else:
            record["answer"] = {
                "type": "harness_config",
                "value": {"answer": answer_json()},
            }
        return {"records": [record]}

    def kinds(self) -> list[str]:
        return [body["question"]["kind"] for _m, body, _t in self.calls]


def _host(engine: Engine | None) -> tuple[Any, RecordingAuthority]:
    host = build_host_context(lambda: None)
    if engine is not None:
        bind_a2a_decision(
            host,
            SimpleNamespace(
                engine_send=engine,
                decision_feature_schema=lambda tenant: SCHEMA,
                decision_principal=lambda: HOST,
            ),
        )
    authority = RecordingAuthority()
    return dataclasses.replace(host.runs.a2a, authority=authority), authority


def _post(service: Any, *, agent: str | None, budget: int | None) -> Any:
    class Authenticator:
        async def authenticate(self, request: Any, *, scope: str) -> Any:
            return _caller()

    message: dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": "do work"}],
        "messageId": "message-1",
    }
    if agent:
        message["metadata"] = {"agent_name": agent}
    params: dict[str, Any] = {"message": message}
    if budget is not None:
        params["contextBudgetTokens"] = budget
    app = create_a2a_application(service=service, authenticator=Authenticator())
    return TestClient(app).post(
        "/a2a",
        headers=HEADERS,
        json={"jsonrpc": "2.0", "id": "s", "method": "message/send", "params": params},
    )


@pytest.mark.spec("ARBORA-A2A-R017.5")
@pytest.mark.spec("ARBORA-A2A-R017.6")
@pytest.mark.spec("ARBORA-A2A-R023")
def test_a_budgeted_message_admits_through_the_host_composition() -> None:
    engine = Engine()
    service, authority = _host(engine)
    response = _post(service, agent="graph:known", budget=4_096)
    assert response.status_code == 200, response.text
    assert engine.kinds() == ["a2a_route", "harness_config"]
    assert {method for method, _b, _t in engine.calls} == {"Decide"}
    assert {tenant for _m, _b, tenant in engine.calls} == {TENANT}
    assert all(body["feature_schema"] == SCHEMA for _m, body, _t in engine.calls)
    [dispatched] = authority.dispatched
    assert dispatched["harness"].admitted_tools == tuple(TOOLS)
    assert dispatched["harness"].decision_record_ref == "record:harness_config:1"
    harness = engine.calls[1][1]
    assert {p["name"]: p["value"]["value"] for p in harness["params"]} == {
        "model_profile_ref": "profile:one",
        "task_class": "task:chat",
        "usable_context_budget_tokens": 4_096,
        "priority": "interactive",
    }


@pytest.mark.spec("ARBORA-A2A-R017.5")
@pytest.mark.spec("ARBORA-A2A-R023")
def test_an_unbound_decision_port_refuses_closed() -> None:
    service, authority = _host(None)
    response = _post(service, agent="graph:known", budget=4_096)
    error = response.json()["error"]
    assert (response.status_code, error["code"]) == (503, -32003)
    assert error["data"] == {"reason": "A2A_ROUTE_UNAVAILABLE"}
    assert authority.dispatched == []
    # No agent name keeps the default agent, which carries no capability: a budget
    # then refuses at admission, never by a guessed profile.
    bare = _post(service, agent=None, budget=4_096)
    assert bare.json()["error"]["data"] == {"reason": "HARNESS_CONFIG_UNAVAILABLE"}
    assert authority.dispatched == []


@pytest.mark.spec("ARBORA-A2A-R016.5")
def test_a_route_record_without_the_typed_capability_asks_no_harness_question() -> None:
    engine = Engine(typed_route=False)
    service, authority = _host(engine)
    response = _post(service, agent="graph:known", budget=4_096)
    assert response.json()["error"]["data"] == {"reason": "HARNESS_CONFIG_UNAVAILABLE"}
    assert engine.kinds() == ["a2a_route"]
    assert authority.dispatched == []
