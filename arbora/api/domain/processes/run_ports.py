"""The ports one process run binds: decision port, approval waiter, run evidence (ARBORA-OPS-R898.5).

The decision port sends ``Decide`` ``BranchSelect`` and ``DecisionTableEval`` to the engine as
the verified caller; arbora decides nothing about a branch or a decision table. The approval
waiter creates the one pending record of the step in the :class:`ApprovalBroker` and reads it
back until ``process.approval.decide`` resolves it. Time comes from the injected ``wait`` and
``now_ms``, never the wall clock.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.api.domain.processes.approvals import ApprovalBroker

Wait = Callable[[float], Awaitable[None]]


class BrokerApprovalWaiter:
    """The ``decided`` half of the harness ``ApprovalWaiter``: pending record, then verdict."""

    def __init__(
        self,
        broker: ApprovalBroker,
        tenant: str,
        wait: Wait,
        now_ms: Callable[[], int],
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self._broker = broker
        self._tenant = tenant
        self._wait = wait
        self._now_ms = now_ms
        self._poll = poll_seconds

    async def decided(
        self, run_id: str, task_id: str, approver_role: str, plan_ref: str
    ) -> Mapping[str, Any]:
        await self._broker.wait(
            self._tenant,
            run_id=run_id,
            step_id=task_id,
            approver_role=approver_role,
            requested_ms=self._now_ms(),
        )
        while True:
            record = await self._broker.read(self._tenant, run_id, task_id)
            if record is not None and record.get("state") == "decided":
                return {
                    "verdict": record["verdict"],
                    "approver": record.get("approver", ""),
                    "decided_at_ms": int(record.get("decided_ms") or 0),
                }
            await self._wait(self._poll)


class RunEvidence:
    """The record ids a run cites: each branch verdict and each decision record."""

    def __init__(self) -> None:
        self.branch_verdicts: list[str] = []
        self.decision_records: list[str] = []

    def note(self, question: str, record_id: str) -> None:
        if not record_id:
            return
        target = (
            self.branch_verdicts
            if question == "BranchSelect"
            else self.decision_records
        )
        target.append(record_id)


def recording(
    port: Callable[[str, Mapping[str, Any], str], Awaitable[Any]],
    evidence: RunEvidence,
) -> Callable[[str, Mapping[str, Any], str], Awaitable[Any]]:
    """The decision port, noting each answered record id in the run evidence."""

    async def decide(question: str, payload: Mapping[str, Any], task_id: str) -> Any:
        verdict = await port(question, payload, task_id)
        evidence.note(question, str(getattr(verdict, "record_id", "") or ""))
        return verdict

    return decide


def process_host_ports(
    *,
    call: Callable[..., Awaitable[Mapping[str, Any]]],
    tenant: str,
    graph: str,
    schema_of: Callable[[str, str], Mapping[str, Any] | None] | None,
    broker: ApprovalBroker,
    wait: Wait,
    now_ms: Callable[[], int],
    evidence: RunEvidence,
) -> Any:
    """The ``HostPorts`` of one process run, or the ``PinLagUnavailable`` naming the gap."""
    from arbora.api.domain.agent_host.host_port_adapters import (
        approval_waiter,
        bind_host_ports,
        decision_port,
    )

    waiter = BrokerApprovalWaiter(broker, tenant, wait, now_ms)
    bound = bind_host_ports(
        decision=recording(
            decision_port(
                call,
                tenant_of=lambda _ref: tenant,
                schema_of=schema_of,
                graph_of=lambda _ref: graph,
            ),
            evidence,
        ),
        approval=approval_waiter(waiter.decided, wait),
    )
    if bound.refusal is not None:
        raise bound.refusal
    return bound.ports


__all__ = [
    "BrokerApprovalWaiter",
    "RunEvidence",
    "process_host_ports",
    "recording",
]
