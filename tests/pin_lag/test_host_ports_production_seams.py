"""The production seams of the composed host ports, reached through the served path.

Each test composes the ports the way ``bind_run_ports`` does (no explicit backends) and
drives one seam: the approval lease read through ``approvals.get`` as the run principal,
the sandbox process table under the action policy, the run-keyed confirm over the A2A
grant, and the terminal outcome built from the run state machine's terminal event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.agent_host.host_port_composition import compose_host_ports
from arbora.agent_host.port_backends import (
    TERMINALS,
    HostSeams,
    RunConfirmExchange,
    RunTerminalEvents,
    TerminalOutcomeUnavailable,
    terminal_extension_supplier,
)
from arbora.agent_runs import NativeRunContext
from arbora.api.invoke.plan import approval_id_for
from arbora.security import request_identity
from tests.api._served_support import capture_invocation, person
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401
from tests.test_served_operation_api import served  # noqa: F401

RUN = "run:seam-2"
PLAN_REF = "arbora_plan:abc123"
IDENTITY = {
    "capability_digest": "cap",
    "catalog_digest": "cat",
    "model_digest": "mod",
    "policy_digest": "pol",
    "delegator_id": "person:one",
    "selected_agent_id": "agent:a",
    "worker_id": "w1",
    "lease_epoch": 3,
    "fencing_token": 9,
}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


def _owner(claim: Mapping[str, Any] | None = None) -> NativeRunContext:
    session = request_identity.mint_graph_session(
        person("one", extra_scopes=("approvals:read",))
    )
    return NativeRunContext(
        run_id=RUN,
        task_id=RUN,
        context_id="ctx",
        work_item_id="work:1",
        session=session,
        claim=claim if claim is not None else IDENTITY,
    )


def _lease(status: str, *, proposer: str = "person:one", expires: int = 5_000) -> Any:
    def handle(params: dict[str, Any], principal: str) -> Any:
        return {
            "lease_id": approval_id_for(PLAN_REF),
            "kind": "action.approval",
            "status": status,
            "revision": 1,
            "issued_at_ms": 1,
            "expires_at_ms": expires,
            "hard_expires_at_ms": expires,
            "grant": {
                "request_digest": "d",
                "op_id": "publish.post",
                "proposer": proposer,
                "gate_class": "publishing",
                "approver_roles": ["editors"],
            },
        }

    return handle


@pytest.mark.spec("ARBORA-OPS-R890.1")
@pytest.mark.pin_lag
def test_production_lease_reader_decides_the_waiter_through_approvals_get(
    captured: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _http, client = served
    answers = {"status": "consumed"}
    reads: list[str] = []

    async def get(*, tenant: str, lease_id: str) -> Any:
        reads.append(lease_id)
        if lease_id != approval_id_for(PLAN_REF):
            return None
        return _lease(answers["status"])({}, "person:one")

    # The engine's control-lease read, the one lower boundary double of this test.
    monkeypatch.setattr(
        client, "control_leases", SimpleNamespace(get=get), raising=False
    )
    owner = _owner()
    ports = compose_host_ports(
        captured["invocation"].services,
        wait=lambda seconds: asyncio.sleep(0),
        resolve_run=lambda ref: owner if ref == RUN else None,
        seams=HostSeams(clock_ms=lambda: 1_000),
    ).ports
    assert ports is not None and ports.approval is not None

    async def verdict(status: str) -> Any:
        answers["status"] = status
        return await ports.approval(RUN, RUN, "editors", 30, PLAN_REF)

    approved = asyncio.run(verdict("consumed"))
    denied = asyncio.run(verdict("revoked"))
    assert approved.verdict == "approve" and denied.verdict == "reject"
    # A foreign run answers like an absent inbox item: nothing is granted.
    foreign = asyncio.run(ports.approval("run:other", RUN, "editors", 30, PLAN_REF))
    assert foreign.verdict == "reject"
    assert reads == [approval_id_for(PLAN_REF)] * 2


@pytest.mark.spec("ARBORA-OPS-R890.2")
@pytest.mark.pin_lag
def test_production_process_table_refuses_without_policy_and_isolation(
    captured: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
) -> None:
    from arbora.agent_host.port_backends import sandbox_process_table
    from arbora.agent_host.port_services import SandboxProcessRefused

    ports = compose_host_ports(
        captured["invocation"].services, wait=lambda s: asyncio.sleep(0)
    ).ports
    assert ports is not None and ports.sandbox_processes is not None
    assert tuple(ports.sandbox_processes(RUN)) == ()

    decisions: list[tuple[str, str]] = []

    class Policy:
        def evaluate(self, request: Any) -> Any:
            decisions.append((request.kind, request.actor_id))
            return SimpleNamespace(
                allowed=request.kind.endswith("terminate") or request.target == "true"
            )

    unverified = sandbox_process_table(lambda: Policy(), None)
    denied = sandbox_process_table(None, lambda: True)

    async def scenario() -> list[str]:
        codes = []
        for table, argv in ((unverified, ["true"]), (denied, ["true"])):
            try:
                await table.spawn(RUN, argv)
            except SandboxProcessRefused as refusal:
                codes.append(refusal.code)
        table = sandbox_process_table(lambda: Policy(), lambda: True)
        process = await table.spawn(RUN, ["true"])
        assert process.process_id.startswith(RUN)
        await process.kill()
        return codes

    codes = asyncio.run(scenario())
    assert codes == ["SANDBOX_UNVERIFIED", "ADMISSION_DENIED"]
    assert ("sandbox_process_spawn", RUN) in decisions


@pytest.mark.spec("ARBORA-OPS-R890.5")
@pytest.mark.pin_lag
def test_production_confirm_is_the_human_grant_and_closed_without_a_chain(
    captured: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
) -> None:
    owner = _owner()
    services = captured["invocation"].services
    closed = compose_host_ports(
        services,
        wait=lambda s: asyncio.sleep(0),
        resolve_run=lambda ref: owner,
    ).ports
    assert closed is not None and closed.confirm is None

    issued: list[dict[str, Any]] = []
    state: dict[str, Any] = {"view": None}

    class Grants:
        async def get(self, tenant: str, grant_id: str) -> Any:
            return state["view"]

        async def issue(self, binding: Any, **_kw: Any) -> Any:
            issued.append({"id": binding.grant_id, "call": binding.pending_call_id})
            return SimpleNamespace(grant_id=binding.grant_id, revision=1)

    chain = SimpleNamespace(grants=Grants(), clock_ms=lambda: 10)
    exchange = RunConfirmExchange(
        chain,
        lambda ref: owner if ref == RUN else None,
        lambda run, kind, digest: _plan(run, kind, digest),
    )

    async def scenario() -> list[Mapping[str, Any]]:
        first = await exchange("tool_action", {"tool": "x"}, RUN)
        state["view"] = SimpleNamespace(status="consumed")
        second = await exchange("tool_action", {"tool": "x"}, RUN)
        state["view"] = SimpleNamespace(status="revoked")
        third = await exchange("tool_action", {"tool": "x"}, RUN)
        foreign = await exchange("tool_action", {}, "run:other")
        return [first, second, third, foreign]

    first, second, third, foreign = asyncio.run(scenario())
    assert first == {"verdict": "deny", "refusal_code": "confirm_pending"}
    assert len(issued) == 1
    assert second["verdict"] == "admit" and second["ref"] == issued[0]["id"]
    assert third["refusal_code"] == "confirm_revoked"
    assert foreign["refusal_code"] == "confirm_unavailable"
    bound = compose_host_ports(
        services,
        wait=lambda s: asyncio.sleep(0),
        resolve_run=lambda ref: owner,
        seams=HostSeams(approval_chain=chain, plan_for=lambda *a: _plan(*a)),
    ).ports
    assert bound is not None and bound.confirm is not None


def _plan(owner: Any, kind: str, digest: str) -> Any:
    from arbora.api.invoke.plan import PlanBinding

    return PlanBinding(
        op_id=f"run.confirm.{kind}",
        params_digest=digest,
        principal=owner.session.actor.actor_id,
        tenant=owner.session.tenant,
        policy_revision="p",
        registry_digest="0" * 64,
        effect="write",
        confirm="plan",
    )


@pytest.fixture
def claim_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structural double of medulla ``main``'s claim answer with its identity digests."""
    from medulla.generated import models

    from arbora.pin_lag.medulla import CLAIM_IDENTITY_FIELDS

    class Claim:
        model_fields = dict.fromkeys(CLAIM_IDENTITY_FIELDS)

    monkeypatch.setattr(models, "ClaimWorkItemResult", Claim, raising=False)


@pytest.mark.spec("ARBORA-OPS-R890.4")
@pytest.mark.pin_lag
def test_terminal_outcome_comes_from_the_terminal_event_and_never_a_default(
    captured: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
    claim_shape: None,
) -> None:
    owner = _owner()
    events = RunTerminalEvents()
    supplier = terminal_extension_supplier(
        lambda ref: owner if ref == RUN else None, events, lambda: 77
    )
    with pytest.raises(TerminalOutcomeUnavailable):
        supplier(RUN)
    events.note(
        {
            "type": "graph_complete",
            "run_id": RUN,
            "sequence": 4,
            "result": {"status": "completed", "run_id": RUN},
        }
    )
    built = supplier(RUN)
    bundle, event = built["outcome_bundle"], built["run_event"]
    assert bundle["run_id"] == event["run_id"] == RUN
    assert bundle["event_sequence"] == 4 and bundle["fence_token"] == 9
    assert bundle["model_digest"] == "mod" and bundle["outcome"] == "completed"
    assert event["kind"] == "terminal" and event["timestamp_ms"] == 77
    # The event is read once; a second read has nothing to commit.
    with pytest.raises(TerminalOutcomeUnavailable):
        supplier(RUN)
    # A claim without its identity digests builds nothing.
    bare = _owner({"fencing_token": 1, "lease_epoch": 1})
    events.note(
        {
            "type": "graph_complete",
            "run_id": RUN,
            "sequence": 1,
            "result": {"status": "completed"},
        }
    )
    with pytest.raises(TerminalOutcomeUnavailable):
        terminal_extension_supplier(lambda ref: bare, events)(RUN)
    # The composed trace sink exists once the supplier does, and the sink feeds TERMINALS.
    ports = compose_host_ports(
        captured["invocation"].services, wait=lambda s: asyncio.sleep(0)
    ).ports
    assert ports is not None and ports.run_trace is not None
    assert isinstance(TERMINALS, RunTerminalEvents)
