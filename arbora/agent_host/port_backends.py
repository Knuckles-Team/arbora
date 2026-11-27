"""Production seams behind the composed harness host ports (ARBORA-OPS-R890.1-R890.6).

Each seam is built over a backend the host already holds and fails closed when that
backend is not composed, so the harness refuses the port instead of Arbora faking it:

* the approval lease reader is the served ``approvals.get`` read, as the run principal;
* the sandbox process table is admitted by the action policy and refuses spawn until the
  host declares a verified isolation boundary;
* the confirm exchange issues and reads the human grant of the A2A approval chain;
* the terminal outcome is built from the run state machine's ``graph_complete`` event and
  the identity digests of the run's claim, never from a default.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from arbora.agent_host.host_port_adapters import RunCall
from arbora.agent_host.port_services import LeaseReader, SandboxProcessTable

_STATUS_OF_STATE: Mapping[str, str] = {
    "approved": "consumed",
    "denied": "revoked",
    "pending": "active",
    "expired": "active",
}
CLAIM_IDENTITY = (
    "capability_digest",
    "catalog_digest",
    "model_digest",
    "policy_digest",
    "delegator_id",
    "selected_agent_id",
)
MAX_TRACKED_RUNS = 256


class TerminalOutcomeUnavailable(RuntimeError):
    """The run's terminal outcome cannot be built; no trace is committed."""


def wall_clock_ms() -> int:
    return int(time.time() * 1000)


def approval_lease_reader(call: RunCall) -> LeaseReader:
    """Read one inbox item through ``approvals.get`` as the run principal.

    The item is projected to the lease shape the waiter reads. An item the run principal
    cannot see answers like an absent one. The served read carries neither the approver
    nor the decision time (see the shape request in the gaps list), so a verdict names no
    approver until the engine read does.
    """

    async def read(run_id: str, approval_id: str) -> Mapping[str, Any] | None:
        answer = await call("approvals.get", {"approval_id": approval_id}, run_id)
        if answer.get("outcome") != "answered":
            return None
        item = (answer.get("payload") or {}).get("item") or {}
        status = _STATUS_OF_STATE.get(str(item.get("state", "")))
        if status is None:
            return None
        expires = int(item.get("expires_at_ms") or 0)
        lease: dict[str, Any] = {"status": status, "expires_at_ms": expires}
        if item.get("state") == "expired":
            # A lapsed item stays active in the engine; the deadline reads as passed.
            lease["expires_at_ms"] = expires or 1
            lease["hard_expires_at_ms"] = expires or 1
        for name in ("decided_by", "updated_at_ms"):
            if item.get(name) is not None:
                lease[name] = item[name]
        return lease

    return read


def sandbox_process_table(
    action_policy: Callable[[], Any] | None,
    sandbox_verified: Callable[[], bool] | None,
) -> SandboxProcessTable:
    """The per-run table, admitted by the action policy.

    Without a composed policy every action is refused, and without a declared isolation
    boundary spawn is refused ``SANDBOX_UNVERIFIED``; listing stays a read of this run's
    own handles.
    """

    def admit(run_id: str, action: str, subject: str) -> bool:
        policy = action_policy() if action_policy is not None else None
        if policy is None:
            return False
        from arbora.access.action_policy import ActionRequest

        decision = policy.evaluate(
            ActionRequest(
                kind=f"sandbox_process_{action}",
                target=subject,
                source="run",
                actor_id=run_id,
            )
        )
        return bool(decision.allowed)

    return SandboxProcessTable(admit, sandbox_verified or (lambda: False))


class RunConfirmExchange:
    """``(kind, payload, run_ref)`` to a verdict over the A2A human grant.

    The pending call is keyed by the run task and the digest of the kind and payload, so a
    retry of one question reads one grant and never asks a human twice. A grant the human
    has signed and consumed is an admit carrying the grant id as its reference; a missing
    grant is issued for the human and answers ``confirm_pending``; anything else denies.
    """

    def __init__(
        self,
        chain: Any,
        resolve_run: Callable[[str], Any],
        plan_for: Callable[[Any, str, str], Any],
    ) -> None:
        self._chain = chain
        self._resolve = resolve_run
        self._plan_for = plan_for

    async def __call__(
        self, kind: str, payload: Mapping[str, Any], run_ref: str
    ) -> Mapping[str, Any]:
        from arbora.a2a.confirm import emit_input_required, grant_id_for

        owner = self._resolve(run_ref)
        if owner is None or owner.run_id != run_ref:
            return {"verdict": "deny", "refusal_code": "confirm_unavailable"}
        body = json.dumps(dict(payload), sort_keys=True, default=repr)
        digest = hashlib.sha256(f"{kind}\0{body}".encode()).hexdigest()
        tenant = str(owner.session.tenant)
        grant_id = grant_id_for(owner.task_id, digest)
        view = await self._chain.grants.get(tenant, grant_id)
        if view is None:
            await emit_input_required(
                self._chain,
                task_id=owner.task_id,
                parent_work_item_id=owner.work_item_id,
                child_work_item_id=f"{owner.work_item_id}:confirm:{digest}",
                pending_call_id=digest,
                plan_ref=f"run-confirm:{digest}",
                plan=self._plan_for(owner, kind, digest),
            )
            return {"verdict": "deny", "refusal_code": "confirm_pending"}
        if view.status == "consumed":
            return {"verdict": "admit", "ref": grant_id}
        return {"verdict": "deny", "refusal_code": f"confirm_{view.status}"}


class RunTerminalEvents:
    """The run state machine's terminal event, held from emission to the trace commit.

    One entry per executing run: the harness emits ``graph_complete`` and then hands the
    trace, so the event is held only between those two calls and read once.
    """

    def __init__(self, limit: int = MAX_TRACKED_RUNS) -> None:
        self._limit = limit
        self._events: OrderedDict[str, Mapping[str, Any]] = OrderedDict()

    def note(self, event: Mapping[str, Any]) -> None:
        run_id = event.get("run_id")
        if event.get("type") != "graph_complete" or not isinstance(run_id, str):
            return
        self._events[run_id] = dict(event)
        while len(self._events) > self._limit:
            self._events.popitem(last=False)

    def take(self, run_id: str) -> Mapping[str, Any] | None:
        return self._events.pop(run_id, None)


#: The process's one holder, fed by the run event sink.
TERMINALS = RunTerminalEvents()


class ProcessTableHolder:
    """The host's per-run sandbox process table, set when the host ports compose."""

    def __init__(self) -> None:
        self.table: SandboxProcessTable | None = None

    def bind(self, table: SandboxProcessTable | None) -> None:
        self.table = table

    def current(self) -> SandboxProcessTable | None:
        return self.table


#: The process's one holder; ``ops.process.terminate`` ends processes through it.
PROCESS_TABLE = ProcessTableHolder()


def terminal_extension_supplier(
    owner_of: Callable[[str], Any],
    events: RunTerminalEvents,
    clock_ms: Callable[[], int] = wall_clock_ms,
) -> Callable[[str], Mapping[str, Any]]:
    """``terminal_extension(run_id)``: the outcome bundle and run event of a finished run.

    Identity digests come from the run's claim and the sequence from the terminal event;
    one that is absent refuses, so no receipt is ever built from a default.
    """

    def build(run_id: str) -> Mapping[str, Any]:
        from arbora.pin_lag.medulla import require_claim_identity

        require_claim_identity()
        owner = owner_of(run_id)
        event = events.take(run_id)
        if owner is None or event is None:
            raise TerminalOutcomeUnavailable("terminal event is not available")
        result = event.get("result")
        if not isinstance(result, Mapping) or result.get("status") != "completed":
            raise TerminalOutcomeUnavailable("only a completed run commits a trace")
        claim = owner.claim
        missing = [name for name in CLAIM_IDENTITY if not claim.get(name)]
        sequence = event.get("sequence")
        if missing or type(sequence) is not int or sequence < 0:
            raise TerminalOutcomeUnavailable("terminal identity is incomplete")
        identity: dict[str, Any] = {
            **{name: str(claim[name]) for name in CLAIM_IDENTITY},
            "completeness": "complete",
            "delegation_id": str(claim.get("delegation_id") or owner.task_id),
            "event_sequence": sequence,
            "executor_lease_actor": str(claim.get("worker_id") or ""),
            "fence_token": int(claim["fencing_token"]),
            "outbox_id": f"outbox:terminal:{run_id}",
            "outcome": "completed",
            "run_id": run_id,
            "schema_version": 1,
            "work_item_id": owner.work_item_id,
        }
        body = json.dumps(dict(event), sort_keys=True, default=repr).encode()
        digest = hashlib.sha256(body).hexdigest()
        return {
            "outcome_bundle": {
                **identity,
                "outcome_ref": f"sha256:{digest}",
                "result_digest": digest,
                "trace_ref": f"trace:{run_id}",
            },
            "run_event": {
                **identity,
                "carrier_digest": digest,
                "cursor_token": f"terminal:{run_id}",
                "kind": "terminal",
                "payload_digest": digest,
                "timestamp_ms": clock_ms(),
            },
        }

    return build


def encode_step(step: Any) -> bytes:
    """The canonical body of one receipt: sorted-key JSON, the one encoding both ends hash."""
    return json.dumps(
        step, sort_keys=True, default=repr, separators=(",", ":")
    ).encode()


@dataclass(frozen=True, slots=True)
class HostSeams:
    """What the host process holds for the ports; an absent one leaves its port closed."""

    action_policy: Callable[[], Any] | None = None
    sandbox_verified: Callable[[], bool] | None = None
    approval_chain: Any = None
    plan_for: Callable[[Any, str, str], Any] | None = None
    summary_model: (
        Callable[[Mapping[str, Any], str], Awaitable[Mapping[str, Any]]] | None
    ) = None
    clock_ms: Callable[[], int] = wall_clock_ms
    changeset: Any = None
    #: ``(tenant, question id)`` to the published feature-schema pin of that question.
    question_schema: Callable[[str, str], Mapping[str, Any] | None] | None = None


def production_backends(
    call: RunCall,
    resolve_run: Callable[[str], Any],
    terminals: RunTerminalEvents,
    seams: HostSeams | None = None,
) -> Any:
    """The ``PortBackends`` of one host: every seam the host can stand behind."""
    from arbora.agent_host.host_port_composition import PortBackends

    seams = seams or HostSeams()
    confirm = None
    if seams.approval_chain is not None and seams.plan_for is not None:
        confirm = RunConfirmExchange(seams.approval_chain, resolve_run, seams.plan_for)
    return PortBackends(
        read_lease=approval_lease_reader(call),
        now_ms=seams.clock_ms,
        processes=sandbox_process_table(seams.action_policy, seams.sandbox_verified),
        summary_model=seams.summary_model,
        confirm_exchange=confirm,
        terminal_extension=terminal_extension_supplier(
            resolve_run, terminals, seams.clock_ms
        ),
        encode=encode_step,
        changeset=seams.changeset,
        sandbox_verified=seams.sandbox_verified,
    )
