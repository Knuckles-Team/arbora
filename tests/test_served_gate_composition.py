"""The served composition binds the gate question and the promotion pipeline read.

``compose_served_invocation`` passes ``gate_send`` to the invoke services and binds
``promotion_ci_tool`` for the promotion handlers (ARBORA-OPS-R918.2,
ARBORA-DEPLOY-R017.23). A request through the served services reaches the engine
gate question; an engine that does not declare it refuses ``UNAVAILABLE`` naming it
and the plan falls back to the strict gate.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from arbora.access.action_policy import ActionPolicies
from arbora.api.invoke.gate_class import NO_APPROVAL, STRICT_DEFAULT, resolve_gate
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.served_invocation import (
    _promotion_ci_tool,
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.security import request_identity
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.key_cache import JwksCache
from tests.test_served_operation_api import FakeClient, FakePool

CI_TOOL = {"server": "ci", "tool": "ci_status"}


class GateClient(FakeClient):
    """The engine fake answering the approval gate question from a table."""

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        super().__init__()
        self.answers = answers

    async def _send(
        self, method: str, params: Any, graph: str | None, **kw: Any
    ) -> Any:
        if method != "ApprovalGateResolve":
            return await super()._send(method, params, graph, **kw)
        self.sent.append((method, params, graph))
        return self.answers[params["request"]["operation"]]


def _declare(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    """The structural double of an engine that does or does not declare the method."""
    import medulla.generated as generated
    from medulla.generated import _runtime

    for module in (generated, _runtime):
        ids = frozenset(module.METHOD_IDS) - {"ApprovalGateResolve"}
        monkeypatch.setattr(
            module, "METHOD_IDS", ids | ({"ApprovalGateResolve"} if present else set())
        )


def _compose(
    client: GateClient,
    monkeypatch: pytest.MonkeyPatch,
    bindings: Mapping[str, Any] = MappingProxyType({}),
) -> Any:
    _declare(monkeypatch, present=True)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "arbora-fixture")
    monkeypatch.setenv("KG_POLICY_VERSION", "policy:fixture")
    actor = ActorContext(
        actor_id="person:one",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
        roles=("kg:read",),
        credential_expires_at=int(time.time()) + 3600,
    )
    process = request_identity.mint_graph_session(actor)
    return compose_served_invocation(
        config=RuntimeConfig(),
        keys=JwksCache(),
        pool=FakePool(client),
        graph=process.graph,
        process_session=lambda: process,
        action_policies=ActionPolicies(),
        bindings=dict(bindings),
        fleet_effect=refused_fleet_effect,
        promotion_ci_tool=CI_TOOL,
    )


# spec: ARBORA-OPS-R918.2
def test_served_services_read_the_gate_from_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GateClient(
        {
            "pay.invoice": {
                "gate": "single",
                "gate_class": "money_movement",
                "approver_roles": ["finance"],
                "expires_after_ms": 30_000,
            },
            "free.op": {"gate": "none", "gate_class": None, "approver_roles": []},
            "open.op": {"gate": "unresolved", "gate_class": None, "approver_roles": []},
        }
    )
    services = _compose(client, monkeypatch).services

    async def gate(op: str) -> Any:
        return await resolve_gate(services.gate_reader, op, "tenant:one")

    paid = asyncio.run(gate("pay.invoice"))
    assert (paid.gate_class, paid.approver_roles, paid.source) == (
        "money_movement",
        ("finance",),
        "engine",
    )
    assert asyncio.run(gate("free.op")) == NO_APPROVAL
    assert asyncio.run(gate("open.op")) == STRICT_DEFAULT
    assert [m for m, *_ in client.sent] == ["ApprovalGateResolve"] * 3


# spec: ARBORA-OPS-R918.2
def test_served_gate_question_refuses_unavailable_naming_the_method_on_an_old_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GateClient({})
    invocation = _compose(client, monkeypatch)
    _declare(monkeypatch, present=False)
    from arbora.api.invoke.pipeline import OperationRefused

    with pytest.raises(OperationRefused) as refused:
        asyncio.run(invocation.services.gate_reader("any.op", "tenant:one"))
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "ApprovalGateResolve"
    # The plan mint reads the refusal as no answer: the strict gate applies.
    strict = asyncio.run(resolve_gate(invocation.services.gate_reader, "any.op", "t"))
    assert strict == STRICT_DEFAULT and client.sent == []


# spec: ARBORA-DEPLOY-R017.23
def test_served_composition_binds_the_promotion_pipeline_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = _compose(GateClient({}), monkeypatch).services
    assert services.runtime.bindings["promotion_ci_tool"] == CI_TOOL

    monkeypatch.setenv("ARBORA_PROMOTION_CI_SERVER", "ci")
    monkeypatch.setenv("ARBORA_PROMOTION_CI_TOOL", "ci_status")
    assert _promotion_ci_tool() == CI_TOOL
    monkeypatch.delenv("ARBORA_PROMOTION_CI_SERVER")
    assert _promotion_ci_tool() is None
