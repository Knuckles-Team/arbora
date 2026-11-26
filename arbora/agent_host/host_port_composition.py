"""The served composition of the harness host ports, over backends Arbora already has.

Each adapter in :mod:`arbora.agent_host.host_port_adapters` is handed one backend
callable here. The run-principal call is the registry ``invoke`` under the verified
session of the run that is executing; it never uses a process or service principal and
answers a run that is not executing exactly like an unknown one. A port whose backend
does not exist yet is left unbound, and the harness refuses it closed (see
``/specs/hosted-api-operations`` rows R890 to R894 and the gaps list).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from arbora.agent_host.host_port_adapters import (
    ChangesetSeams,
    EngineDecisionAdapter,
    HostPortsBinding,
    RunCall,
    approval_waiter,
    bind_host_ports,
    changeset_submitter_factory,
    decision_port,
    gate_runner_factory,
    liveness_wait,
    plan_confirmer,
    run_trace_sink,
    sandbox_process_provider,
    study_content_reader,
    summary_provider,
    technique_measure,
)
from arbora.agent_host.port_services import (
    ApprovalInboxWaiter,
    RunTraceReceipts,
    RunVerdictConfirm,
    SandboxProcessTable,
    StudyContentService,
    SummaryService,
    TechniqueMeasureService,
)


def _run_of(run_ref: str) -> Any:
    from arbora.agent_runs import current_native_run

    return current_native_run(SimpleNamespace(run_id=run_ref))


def _run_key(op_id: str, params: Mapping[str, Any], run_ref: str) -> str:
    """One stable key per run, operation and body: a replayed call writes once."""
    body = json.dumps(params, sort_keys=True, default=repr, separators=(",", ":"))
    return "run:" + hashlib.sha256(f"{run_ref}\0{op_id}\0{body}".encode()).hexdigest()


def run_principal_call(
    services: Any,
    resolve_run: Callable[[str], Any] = _run_of,
) -> RunCall:
    """``RunCall`` over registry ``invoke`` as the executing run's verified principal."""

    async def call(
        op_id: str, params: Mapping[str, Any], run_ref: str
    ) -> Mapping[str, Any]:
        from arbora.api.invoke.pipeline import invoke
        from arbora.api.invoke.steps import OpError
        from arbora.api.mcp.caller import caller_from_session
        from arbora.api.registry import Surface

        try:
            owner = resolve_run(run_ref)
        except RuntimeError:
            owner = None
        if owner is None or owner.run_id != run_ref:
            # A foreign, closed or unknown run answers alike.
            return {"outcome": "refused", "refusal_code": "UNAVAILABLE"}
        from arbora.security.session import use_session

        caller = caller_from_session(owner.session, credential_kind="session")
        with use_session(owner.session):
            result = await invoke(
                op_id,
                dict(params),
                caller,
                Surface.MCP,
                services=services,
                idempotency_key=_run_key(op_id, params, run_ref),
            )
        if isinstance(result, OpError):
            return {
                "outcome": "refused",
                "refusal_code": result.code,
                "payload": dict(result.details),
            }
        value = result.value
        return {
            "outcome": "answered",
            "payload": dict(value) if isinstance(value, Mapping) else {"value": value},
        }

    return call


def active_run_ref() -> str:
    """The run id of the executing run scope, for ports that carry no run reference."""
    from arbora.agent_runs import _NATIVE_RUN

    scope = _NATIVE_RUN.get()
    if scope is None or not scope.active:
        raise RuntimeError("no run is executing")
    return str(scope.ownership.run_id)


@dataclass(frozen=True, slots=True)
class PortBackends:
    """The host seams the composed ports stand on; a missing one leaves its port unbound."""

    read_lease: Callable[[str, str], Awaitable[Mapping[str, Any] | None]] | None = None
    now_ms: Callable[[], int] | None = None
    processes: SandboxProcessTable | None = None
    summary_model: (
        Callable[[Mapping[str, Any], str], Awaitable[Mapping[str, Any]]] | None
    ) = None
    confirm_exchange: (
        Callable[[str, Mapping[str, Any], str], Awaitable[Mapping[str, Any]]] | None
    ) = None
    terminal_extension: Callable[[str], Mapping[str, Any]] | None = None
    encode: Callable[[Any], bytes] | None = None
    changeset: ChangesetSeams | None = None
    sandbox_verified: Callable[[], bool] | None = None


def compose_host_ports(
    services: Any,
    *,
    wait: Callable[[float], Any],
    checkpoint_store: Any = None,
    resolve_run: Callable[[str], Any] = _run_of,
    method_op: Callable[[str], str | None] = lambda _: None,
    backends: PortBackends | None = None,
    seams: Any = None,
) -> HostPortsBinding:
    """Bind every port that has a backend; the pinned release refuses ``UNAVAILABLE``.

    Without explicit ``backends`` the production seams are built over ``seams`` (the host's
    ``HostSeams``), and each seam the host cannot stand behind leaves its port closed.
    """
    call = run_principal_call(services, resolve_run)
    if backends is None:
        from arbora.agent_host.port_backends import TERMINALS, production_backends

        backends = production_backends(call, resolve_run, TERMINALS, seams)

    def tenant_of(run_ref: str) -> str:
        owner = resolve_run(run_ref)
        return "" if owner is None else str(owner.session.tenant)

    ports: dict[str, Any] = {
        "engine_decision": EngineDecisionAdapter(call, method_op),
        "decision": decision_port(call),
        "liveness_wait": liveness_wait(wait),
        "checkpoint_store": checkpoint_store,
        "summary_provider": summary_provider(
            SummaryService(call, backends.summary_model)
        ),
        "study_content": study_content_reader(
            StudyContentService(call, active_run_ref, tenant_of)
        ),
        "technique_measure": technique_measure(
            TechniqueMeasureService(
                call, active_run_ref, tenant_of, backends.now_ms or (lambda: 0)
            )
        ),
    }
    if backends.read_lease is not None and backends.now_ms is not None:
        inbox = ApprovalInboxWaiter(backends.read_lease, wait, backends.now_ms)
        ports["approval"] = approval_waiter(inbox.decided, wait)
    if backends.processes is not None:
        ports["sandbox_processes"] = sandbox_process_provider(backends.processes.list)
    if backends.confirm_exchange is not None:
        ports["confirm"] = plan_confirmer(RunVerdictConfirm(backends.confirm_exchange))
    if backends.terminal_extension is not None and backends.encode is not None:
        receipts = RunTraceReceipts(
            call, resolve_run, backends.terminal_extension, backends.encode
        )
        ports["run_trace"] = run_trace_sink(receipts.commit)
    if backends.changeset is not None and backends.sandbox_verified is not None:
        ports["changeset_submitter"] = changeset_submitter_factory(
            call, backends.changeset, backends.sandbox_verified
        )
        ports["gate_runner"] = gate_runner_factory(
            backends.changeset, backends.sandbox_verified
        )
    return bind_host_ports(**ports)
