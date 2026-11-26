"""The backends behind the harness host ports (ARBORA-OPS-R890.1 to R890.6).

The served-path tests run the services through the registry ``invoke`` as the verified
run principal against a recording engine; the remaining ones run them against the
structural double of the main-branch port shapes.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.agent_host.host_port_composition import (
    PortBackends,
    compose_host_ports,
    run_principal_call,
)
from arbora.agent_host.port_services import (
    ApprovalInboxWaiter,
    RunTraceReceipts,
    RunVerdictConfirm,
    SandboxProcessRefused,
    SandboxProcessTable,
    StudyContentService,
    SummaryService,
    TechniqueMeasureService,
)
from arbora.agent_runs import NativeRunContext
from arbora.security import request_identity
from tests.api._served_support import capture_invocation, person, route_methods
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401
from tests.test_served_operation_api import served  # noqa: F401

RUN = "run:backend-1"
SCOPES = ("query:retrieval", "node:read", "agent:decision-write", "work:write")


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


def _owner(claim: Mapping[str, Any] | None = None) -> NativeRunContext:
    session = request_identity.mint_graph_session(person("one", extra_scopes=SCOPES))
    return NativeRunContext(
        run_id=RUN,
        task_id=RUN,
        context_id="ctx",
        work_item_id="work:1",
        session=session,
        claim=claim or {"lease_epoch": 3, "fencing_token": 9, "worker_id": "w1"},
    )


class Recorder:
    """A recording engine method handler."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[tuple[dict[str, Any], str]] = []

    def __call__(self, params: dict[str, Any], principal: str) -> Any:
        self.requests.append((params, principal))
        return self.answer


def _served_call(
    served_client: Any, monkeypatch: Any, captured: Any, handlers: Any
) -> Any:
    route_methods(
        served_client,
        handlers,
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=SCOPES)},
    )
    owner = _owner()
    return (
        run_principal_call(
            captured["invocation"].services, lambda ref: owner if ref == RUN else None
        ),
        owner,
    )


@pytest.mark.spec("ARBORA-OPS-R890.3")
@pytest.mark.pin_lag
def test_summary_study_and_technique_services_reach_the_engine_as_the_run_principal(
    captured: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _http, client = served
    compiled = Recorder({"bundle": {"items": []}})
    node = Recorder({"properties": {"content": "the study body"}})
    log: list[tuple[str, Mapping[str, Any], str]] = []

    async def measure_call(
        op: str, params: Mapping[str, Any], ref: str
    ) -> Mapping[str, Any]:
        # A governed write needs the effect journal's replay owner, which the served
        # fixture lacks, so the arm read is proven against a recording call.
        log.append((op, params, ref))
        return {
            "outcome": "answered",
            "payload": {
                "rows": [
                    {"option_id": "arm-a", "trials": 4, "successes": 3, "refused": 1}
                ]
            },
        }

    call, owner = _served_call(
        client,
        monkeypatch,
        captured,
        {"ContextCompile": compiled, "GetNodeProperties": node},
    )
    tenant = owner.session.tenant

    async def model(context: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
        assert context["compiled"] is not None
        return {"output": "s", "provider": "p", "model": "m", "policy_revision": "r1"}

    run_ref = lambda: RUN  # noqa: E731

    async def scenario() -> Any:
        summary = await SummaryService(call, model)(
            {"run_id": RUN, "query": "q"}, "adm"
        )
        off = await SummaryService(call, None)({"run_id": RUN}, "adm")
        body = await StudyContentService(call, run_ref, lambda _r: tenant)(
            tenant, "g", "n1", 2
        )
        foreign = await StudyContentService(call, run_ref, lambda _r: tenant)(
            "other-tenant", "g", "n1", 2
        )
        metrics = await TechniqueMeasureService(
            measure_call, run_ref, lambda _r: tenant, lambda: 50
        )("t", "arm-a", "d" * 8)
        return summary, off, body, foreign, metrics

    summary, off, body, foreign, metrics = asyncio.run(scenario())
    assert summary["output"] == "s" and off is None
    assert body == b"the study body" and foreign is None
    assert metrics == {"trials": 4, "successes": 3, "refused": 1, "success_rate": 0.75}
    assert [p for _r, p in compiled.requests + node.requests] == ["person:one"] * 2
    assert node.requests[0][0]["node_id"] == "n1"
    assert log[0][1]["op"]["op"] == "aggregate" and log[0][2] == RUN
    assert log[0][1]["op"]["request"]["tenant_id"] == tenant


@pytest.mark.spec("ARBORA-OPS-R890.1")
def test_approval_waiter_maps_the_run_task_to_its_inbox_item_by_plan_ref() -> None:
    leases: dict[str, Mapping[str, Any]] = {}
    reads: list[str] = []
    ticks: list[float] = []
    now = {"ms": 100}

    async def read(run_id: str, approval_id: str) -> Mapping[str, Any] | None:
        reads.append(approval_id)
        return leases.get(approval_id)

    async def clock(seconds: float) -> None:
        ticks.append(seconds)
        if len(ticks) == 2:
            leases["action_approval:abc"] = {
                "status": "consumed",
                "decided_by": "ann",
                "updated_at_ms": 150,
            }

    inbox = ApprovalInboxWaiter(read, clock, lambda: now["ms"])
    leases["action_approval:abc"] = {
        "status": "active",
        "expires_at_ms": 1000,
        "hard_expires_at_ms": 1000,
    }
    approved = asyncio.run(inbox.decided(RUN, "t1", "owner", "arbora_plan:abc"))
    assert reads[0] == "action_approval:abc" and len(ticks) == 2
    assert approved == {"verdict": "approve", "approver": "ann", "decided_at_ms": 150}
    leases["action_approval:abc"] = {"status": "revoked"}
    assert (
        asyncio.run(inbox.decided(RUN, "t1", "owner", "arbora_plan:abc"))["verdict"]
        == "reject"
    )
    now["ms"] = 5000
    leases["action_approval:abc"] = {
        "status": "active",
        "expires_at_ms": 1000,
        "hard_expires_at_ms": 1000,
    }
    assert (
        asyncio.run(inbox.decided(RUN, "t1", "owner", "arbora_plan:abc"))["verdict"]
        == "timeout"
    )
    # An item nobody filed for this plan reads as a denial, never an approval.
    assert (
        asyncio.run(inbox.decided(RUN, "t1", "owner", "arbora_plan:none"))["verdict"]
        == "reject"
    )


@pytest.mark.spec("ARBORA-OPS-R890.2")
def test_process_table_is_keyed_by_run_and_admitted() -> None:
    admitted: list[tuple[str, str]] = []
    allow = {"on": True}

    def admit(run_id: str, action: str, subject: str) -> bool:
        admitted.append((run_id, action))
        return allow["on"]

    child = [sys.executable, "-c", "import sys; sys.stdin.read()"]

    async def scenario() -> None:
        unverified = SandboxProcessTable(admit, lambda: False)
        with pytest.raises(SandboxProcessRefused) as refused:
            await unverified.spawn("run:a", child)
        assert refused.value.code == "SANDBOX_UNVERIFIED"
        table = SandboxProcessTable(admit, lambda: True)
        first = await table.spawn("run:a", child)
        second = await table.spawn("run:b", child)
        assert [p.process_id for p in table.list("run:a")] == [first.process_id]
        assert await first.alive() and await second.alive()
        # Another run's process answers like an absent one.
        assert await table.terminate("run:a", second.process_id) is False
        allow["on"] = False
        with pytest.raises(SandboxProcessRefused):
            await first.kill()
        with pytest.raises(SandboxProcessRefused):
            await table.spawn("run:a", child)
        allow["on"] = True
        assert await table.terminate("run:a", first.process_id) is True
        assert not await first.alive() and table.list("run:a") == ()
        await table.release("run:b")
        assert not await second.alive() and table.list("run:b") == ()

    asyncio.run(scenario())
    assert ("run:a", "terminate") in admitted and ("run:a", "spawn") in admitted


@pytest.mark.spec("ARBORA-OPS-R890.4")
def test_run_trace_receipts_commit_with_the_run_fence() -> None:
    sent: list[tuple[str, Mapping[str, Any], str]] = []
    owner = SimpleNamespace(
        task_id=RUN,
        work_item_id="work:1",
        session=SimpleNamespace(tenant="t1"),
        claim={"lease_epoch": 3, "fencing_token": 9, "worker_id": "w1"},
    )

    async def call(op: str, params: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
        sent.append((op, params, ref))
        return {"outcome": "answered", "payload": {}}

    receipts = RunTraceReceipts(
        call,
        lambda ref: owner,
        lambda ref: {"outcome_bundle": {"b": 1}, "run_event": {"e": 1}},
        lambda value: repr(value).encode(),
    )
    steps = [{"task_id": "t1", "name": "tool", "outcome": "ok"}]
    asyncio.run(receipts.commit(RUN, steps))
    op, params, ref = sent[0]
    assert op == "eg.coordination.CommitWorkItemResult" and ref == RUN
    assert (params["lease_epoch"], params["fencing_token"]) == (3, 9)
    assert params["idempotency_key"] == f"run-trace:{RUN}"
    nodes = params["outcome_extension"]["receipt_nodes"]
    assert [n["kind"] for n in nodes] == ["run_trace", "tool_call"]
    assert {n["fence_token"] for n in nodes} == {9}
    assert len({n["node_id"] for n in nodes}) == 2
    assert all(n["payload_ref"] == f"sha256:{n['payload_digest']}" for n in nodes)


@pytest.mark.spec("ARBORA-OPS-R890.5")
def test_confirm_admits_only_a_signed_grant() -> None:
    answers = iter(
        [
            {"verdict": "admit", "ref": "grant:1"},
            {"verdict": "admit"},
            {"verdict": "deny", "refusal_code": "GRANT_REVOKED"},
        ]
    )

    async def exchange(
        kind: str, payload: Mapping[str, Any], ref: str
    ) -> Mapping[str, Any]:
        return next(answers)

    confirm = RunVerdictConfirm(exchange)

    async def scenario() -> list[Mapping[str, Any]]:
        return [
            await confirm("tool_action", {}, RUN),
            await confirm("tool_action", {}, RUN),
            await confirm("held_item", {}, RUN),
            await confirm("nonsense", {}, RUN),
        ]

    admitted, unsigned, revoked, unknown = asyncio.run(scenario())
    assert admitted == {"verdict": "admit", "ref": "grant:1"}
    assert (
        unsigned["verdict"] == "deny" and unsigned["refusal_code"] == "confirm_unsigned"
    )
    assert revoked["refusal_code"] == "GRANT_REVOKED"
    assert unknown["refusal_code"] == "confirm_kind_unknown"


@pytest.mark.spec("ARBORA-OPS-R890.6")
@pytest.mark.pin_lag
def test_composition_binds_each_port_that_has_a_backend(
    captured: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
) -> None:
    services = captured["invocation"].services

    async def read(run: str, approval: str) -> None:
        return None

    bare = compose_host_ports(
        services, wait=lambda s: asyncio.sleep(0), backends=PortBackends()
    ).ports
    assert bare is not None
    assert bare.summary_provider is not None and bare.study_content is not None
    assert bare.technique_measure is not None
    assert bare.approval is None and bare.sandbox_processes is None
    assert bare.confirm is None and bare.run_trace is None

    async def exchange(
        kind: str, payload: Mapping[str, Any], ref: str
    ) -> Mapping[str, Any]:
        return {"verdict": "deny"}

    full = compose_host_ports(
        services,
        wait=lambda s: asyncio.sleep(0),
        backends=PortBackends(
            read_lease=read,
            now_ms=lambda: 1,
            processes=SandboxProcessTable(lambda *a: True, lambda: True),
            confirm_exchange=exchange,
            terminal_extension=lambda ref: {},
            encode=lambda value: b"x",
        ),
    ).ports
    for name in ("approval", "sandbox_processes", "confirm", "run_trace"):
        assert getattr(full, name) is not None, name
