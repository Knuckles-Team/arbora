"""The meristem host ports Arbora binds, each over one backend seam it is handed.

Every adapter is a thin translation between a harness port and a backend callable that
the host composition root supplies; none holds state or decides anything. The result
types come from ``arbora.pin_lag.meristem`` so a pinned release without them refuses
``UNAVAILABLE`` naming the missing symbol (PIN-LAG: the guard goes after the re-pin).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arbora.pin_lag.meristem import port_types

#: ``(operation id, params, run ref)`` to the engine result as plain data.
RunCall = Callable[[str, Mapping[str, Any], str], Awaitable[Mapping[str, Any]]]

DECIDE_OP = "eg.query.Decide"
ASSEMBLE_OP = "eg.storage.AgentAssemble"
LOG_OP = "eg.coordination.DecisionLog"


def _answer(raw: Mapping[str, Any]) -> Any:
    types = port_types()
    return types.EngineAnswer(
        outcome=raw["outcome"],
        option_ids=tuple(raw.get("option_ids") or ()),
        refusal_code=raw.get("refusal_code"),
        record_digest=raw.get("record_digest"),
        payload=dict(raw.get("payload") or {}),
    )


class EngineDecisionAdapter:
    """``EngineDecisionPort`` over registered engine operations under the run principal."""

    def __init__(
        self, call: RunCall, method_op: Callable[[str], str | None] = lambda _: None
    ) -> None:
        self._call = call
        self._method_op = method_op

    async def decide(
        self, kind: str, params: Mapping[str, Any], *, run_ref: str
    ) -> Any:
        return _answer(await self._call(DECIDE_OP, {"kind": kind, **params}, run_ref))

    async def assemble(self, request: Mapping[str, Any], *, run_ref: str) -> Any:
        return _answer(await self._call(ASSEMBLE_OP, dict(request), run_ref))

    async def log(self, op: str, body: Mapping[str, Any], *, run_ref: str) -> Any:
        """One call and one atomic engine write: the body travels with the op."""
        return _answer(await self._call(LOG_OP, {"op": op, **body}, run_ref))

    async def read(
        self, method: str, params: Mapping[str, Any], *, run_ref: str
    ) -> Any:
        return await self._method(method, params, run_ref)

    async def write(
        self, method: str, params: Mapping[str, Any], *, run_ref: str
    ) -> Any:
        return await self._method(method, params, run_ref)

    async def _method(
        self, method: str, params: Mapping[str, Any], run_ref: str
    ) -> Any:
        op_id = self._method_op(method)
        if op_id is None:
            raise port_types().EngineDecisionRefused(
                "engine_method_forbidden", method=method
            )
        return _answer(await self._call(op_id, dict(params), run_ref))


def decision_port(
    call: RunCall,
) -> Callable[[str, Mapping[str, Any], str], Awaitable[Any]]:
    """``DecisionPort``: a control-step question answered by the engine decision."""

    async def decide(question: str, payload: Mapping[str, Any], task_id: str) -> Any:
        raw = await call(
            DECIDE_OP, {"kind": question, "task_id": task_id, **payload}, task_id
        )
        return port_types().DecisionVerdict(
            record_id=str(raw.get("record_id") or ""),
            taken_task_ids=tuple(raw.get("taken_task_ids") or ()),
            outputs=raw.get("outputs"),
            abstention=raw.get("abstention"),
        )

    return decide


def approval_waiter(
    decided: Callable[[str, str, str], Awaitable[Mapping[str, Any]]],
    wait: Callable[[float], Awaitable[None]],
) -> Callable[[str, str, str, int], Awaitable[Any]]:
    """``ApprovalWaiter``: the decision races an injected clock; the loser is cancelled."""

    async def wait_for_approval(
        run_id: str, task_id: str, approver_role: str, timeout_s: int
    ) -> Any:
        types = port_types()
        answer = asyncio.ensure_future(decided(run_id, task_id, approver_role))
        timer = asyncio.ensure_future(wait(float(timeout_s)))
        try:
            await asyncio.wait({answer, timer}, return_when=asyncio.FIRST_COMPLETED)
            if not answer.done():
                return types.ApprovalOutcome(verdict="timeout")
            raw = answer.result()
            return types.ApprovalOutcome(
                verdict=raw["verdict"],
                approver=str(raw.get("approver") or ""),
                decided_at_ms=int(raw.get("decided_at_ms") or 0),
            )
        finally:
            for task in (answer, timer):
                task.cancel()

    return wait_for_approval


def plan_confirmer(
    confirm: Callable[[str, Mapping[str, Any], str], Awaitable[Mapping[str, Any]]],
) -> Callable[[str, Mapping[str, Any], str], Awaitable[Any]]:
    """``PlanConfirmer``: any failure of the confirm exchange denies."""

    async def confirmed(kind: str, payload: Mapping[str, Any], run_ref: str) -> Any:
        types = port_types()
        try:
            raw = await confirm(kind, payload, run_ref)
            return types.ConfirmVerdict(
                verdict=str(raw["verdict"]),
                ref=raw.get("ref"),
                refusal_code=raw.get("refusal_code"),
            )
        except Exception:
            return types.ConfirmVerdict(
                verdict="denied", refusal_code="confirm_unavailable"
            )

    return confirmed


def summary_provider(
    summarize: Callable[[Mapping[str, Any], str], Awaitable[Mapping[str, Any] | None]],
) -> Callable[[Mapping[str, Any], str], Awaitable[Any]]:
    """``SummaryProvider``: a disabled provider raises and leaves no receipt."""

    async def summarized(context: Mapping[str, Any], admission_ref: str) -> Any:
        types = port_types()
        raw = await summarize(context, admission_ref)
        if raw is None:
            raise types.SummaryProviderDisabled("summary provider is disabled")
        return types.SummaryResult(
            output=str(raw["output"]),
            provider=str(raw["provider"]),
            model=str(raw["model"]),
            policy_revision=str(raw["policy_revision"]),
        )

    return summarized


def study_content_reader(
    read: Callable[[str, str, str, int], Awaitable[bytes | None]],
) -> Callable[[str, str, str, int], Awaitable[bytes]]:
    """``StudyContentReader``: a source the caller cannot read is absent, not empty."""

    async def body(tenant_id: str, graph: str, node: str, revision: int) -> bytes:
        content = await read(tenant_id, graph, node, revision)
        if content is None:
            raise LookupError("study source not found")
        return bytes(content)

    return body


def run_trace_sink(
    commit: Callable[[str, Sequence[Mapping[str, Any]]], Awaitable[None]],
) -> Callable[[Mapping[str, Any]], Awaitable[None]]:
    """``RunTraceSink``: the ``{run_id, steps}`` trace goes to the receipt writer."""

    async def sink(trace: Mapping[str, Any]) -> None:
        await commit(str(trace["run_id"]), list(trace.get("steps") or ()))

    return sink


def technique_measure(
    measure: Callable[[str, str, str], Awaitable[Mapping[str, Any]]],
) -> Callable[[str, str, str], Awaitable[Mapping[str, Any]]]:
    """``TechniqueMeasure``: metrics must be numbers; anything else refuses."""

    async def measured(
        technique: str, arm: str, config_digest: str
    ) -> Mapping[str, Any]:
        metrics = dict(await measure(technique, arm, config_digest))
        for name, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"metric {name} is not a number")
        return metrics

    return measured


def liveness_wait(
    wait: Callable[[float], Awaitable[None]],
) -> Callable[[float], Awaitable[None]]:
    """``LivenessWait``: the host's injected clock; the harness never sleeps."""

    async def waited(seconds: float) -> None:
        await wait(float(seconds))

    return waited


def sandbox_process_provider(
    processes: Callable[[str], Sequence[Any]],
) -> Callable[[str], Sequence[Any]]:
    """``SandboxProcessProvider``: only the processes bound to this run's sandbox."""

    def bound(run_id: str) -> Sequence[Any]:
        return tuple(processes(run_id))

    return bound


def changeset_submitter_factory(
    build: Callable[[str], Any | None], sandbox_verified: Callable[[], bool]
) -> Callable[[str], Any | None]:
    """``run_id`` to the run's submitter; unverified isolation binds none."""

    def for_run(run_id: str) -> Any | None:
        return build(run_id) if sandbox_verified() else None

    return for_run


def gate_runner_factory(
    build: Callable[[str], Any | None], sandbox_verified: Callable[[], bool]
) -> Callable[[str], Any | None]:
    """``run_id`` to the run's sandbox gate runner; unverified isolation binds none."""

    def for_run(run_id: str) -> Any | None:
        return build(run_id) if sandbox_verified() else None

    return for_run


class HostPortsBinding:
    """The outcome of binding the lane ports: the record, or the refusal naming the gap."""

    __slots__ = ("ports", "refusal")

    def __init__(self, ports: Any | None, refusal: Any | None) -> None:
        self.ports = ports
        self.refusal = refusal


def bind_host_ports(**bound: Any) -> HostPortsBinding:
    """Build the ``HostPorts`` record, or return the ``UNAVAILABLE`` that names the gap."""
    from arbora.pin_lag.meristem import PinLagUnavailable, build_host_ports

    try:
        return HostPortsBinding(build_host_ports(**bound), None)
    except PinLagUnavailable as refusal:
        return HostPortsBinding(None, refusal)
