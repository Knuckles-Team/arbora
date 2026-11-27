"""The production composition of the approval chain (ARBORA-A2A-R005.2.5)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.chain_composition import (
    LEASE_KIND,
    acknowledge,
    audit_sink,
    compose_approval_chain,
    run_plan_binding,
)
from arbora.api.domain.a2a.tool_action_admission import ToolActionAdmitter
from arbora.api.invoke.plan import PlanBinding
from arbora.core.claims import params_digest
from tests.api._support import make_verified_caller  # noqa: F401


async def _run(awaitable: Any) -> Any:
    return await awaitable


class Adapter:
    def __init__(self) -> None:
        self.events: list[tuple[dict[str, str], Any]] = []

    async def preflight(self, event: Any, audit_class: Any, caller: Any) -> str:
        self.events.append((dict(event), caller))
        return "ref"


def _plan() -> PlanBinding:
    return PlanBinding(
        "run.confirm.tool_action",
        params_digest({"v": 1}),
        "person:one",
        "t",
        "p",
        "0" * 64,
        "write",
        "plan",
    )


@pytest.mark.spec("ARBORA-A2A-R005.2.5")
def test_production_chain_is_built_from_the_engine_alone() -> None:
    class Decider:
        async def decide(self, request: Any, *, tenant: str) -> None:
            return None

    async def client() -> Any:
        return object()

    chain = compose_approval_chain(
        leases=SimpleNamespace(control_leases=SimpleNamespace()),
        client=client,
        graph="g",
        tenant="t",
        audit_adapter=Adapter(),
        current_session=lambda: None,
        tool_decider=Decider(),
    )
    assert chain.grants._kind == LEASE_KIND
    assert isinstance(chain.tool_actions, ToolActionAdmitter)
    # Without an engine decider the held-item branch is closed, never defaulted.
    assert chain.held_items is None
    binding = SimpleNamespace(plan=_plan(), grant_id="grant:1")
    assert asyncio.run(acknowledge(binding)) == {  # type: ignore[arg-type]
        "acknowledged": "run.confirm.tool_action",
        "grant_id": "grant:1",
    }
    with pytest.raises(PermissionError):
        asyncio.run(_run(chain.audit({"outcome": "consumed"})))


@pytest.mark.spec("ARBORA-A2A-R005.2.5")
def test_audit_entries_reserve_a_digest_never_the_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = Adapter()
    caller = SimpleNamespace(
        principal="person:one", tenant="t", request_id="r", engine_claims={}
    )
    monkeypatch.setattr(
        "arbora.api.mcp.caller.caller_from_session",
        lambda session, credential_kind, request_id="": SimpleNamespace(
            **{**caller.__dict__, "request_id": request_id}
        ),
    )
    asyncio.run(
        _run(audit_sink(adapter, lambda: object())({"secret_ref": "do-not-leak"}))
    )
    event, _ = adapter.events[0]
    assert event["surface"] == "a2a" and event["op"] == "a2a.plan.confirm"
    assert len(event["plan_digest"]) == 64 and "do-not-leak" not in str(event)


@pytest.mark.spec("ARBORA-A2A-R005.2.5")
def test_run_plan_binding_signs_the_run_confirmation() -> None:
    owner = SimpleNamespace(
        session=SimpleNamespace(
            actor=SimpleNamespace(actor_id="person:one"),
            tenant="t",
            policy_version="p1",
        )
    )
    plan = run_plan_binding(SimpleNamespace(registry=SimpleNamespace(digest="d" * 64)))(
        owner, "tool_action", "e" * 64
    )
    assert plan.principal == "person:one" and plan.confirm == "plan"
    assert plan.op_id == "run.confirm.tool_action" and plan.policy_revision == "p1"
