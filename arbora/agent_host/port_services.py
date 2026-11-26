"""The backends behind the harness host ports that need more than one engine call.

Each service takes the host's run-principal :data:`~arbora.agent_host.host_port_adapters.RunCall`
(registry ``invoke`` as the executing run's verified principal) or an injected seam,
holds no durable state and decides nothing: the engine owns every fact and refusal.
Process handles are the one local thing, because an operating-system process cannot live
in the graph; the table holds only handles this host spawned and admitted.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arbora.agent_host.host_port_adapters import RunCall
from arbora.api.invoke.plan import approval_id_for

COMPILE_OP = "eg.query.ContextCompile"
NODE_OP = "eg.graph.GetNodeProperties"
LOG_OP = "eg.coordination.DecisionLog"
COMMIT_OP = "eg.coordination.CommitWorkItemResult"
CONFIRM_KINDS = frozenset({"tool_action", "held_item", "process_terminate"})
BODY_PROPERTIES = ("content", "body", "text")

#: ``(run id, approval id)`` to the engine lease of that approval, or ``None`` when absent.
LeaseReader = Callable[[str, str], Awaitable[Mapping[str, Any] | None]]
Clock = Callable[[float], Awaitable[None]]


class ApprovalInboxWaiter:
    """Maps a waiting run task to its ``approvals.*`` inbox item by plan reference.

    The inbox item is the engine ``action.approval`` lease whose id derives from the plan
    reference (``approval_id_for``). The wait polls that lease on the injected clock until
    the item is approved (``consumed``) or denied (``revoked``). An absent item and a
    foreign one both read as a denial: nothing is filed for this run, so nothing is granted.
    """

    def __init__(
        self,
        read: LeaseReader,
        wait: Clock,
        now_ms: Callable[[], int],
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self._read = read
        self._wait = wait
        self._now_ms = now_ms
        self._poll = poll_seconds

    async def decided(
        self, run_id: str, task_id: str, approver_role: str, plan_ref: str
    ) -> Mapping[str, Any]:
        approval_id = approval_id_for(plan_ref)
        while True:
            lease = await self._read(run_id, approval_id)
            if lease is None:
                return {"verdict": "reject"}
            status = str(lease.get("status", ""))
            if status in {"consumed", "revoked"}:
                grant = lease.get("grant") or {}
                return {
                    "verdict": "approve" if status == "consumed" else "reject",
                    "approver": str(
                        lease.get("decided_by") or grant.get("approver") or ""
                    ),
                    "decided_at_ms": int(lease.get("updated_at_ms") or self._now_ms()),
                }
            deadline = min(
                int(lease.get("expires_at_ms") or 0),
                int(lease.get("hard_expires_at_ms") or 0),
            )
            if deadline and self._now_ms() >= deadline:
                return {"verdict": "timeout"}
            await self._wait(self._poll)


class SandboxProcessRefused(RuntimeError):
    """The host did not admit the action; ``code`` names why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(eq=False)
class SandboxProcess:
    """One process bound to a run's sandbox: the ``SandboxProcess`` port shape."""

    process_id: str
    run_id: str
    _handle: Any = field(repr=False)
    _admit: Callable[[str, str, str], bool] = field(repr=False)

    async def alive(self) -> bool:
        return bool(self._handle.returncode is None)

    async def kill(self) -> None:
        if not self._admit(self.run_id, "terminate", self.process_id):
            raise SandboxProcessRefused("ADMISSION_DENIED")
        if self._handle.returncode is None:
            self._handle.kill()
        await self._handle.wait()


class SandboxProcessTable:
    """Per-run process table: spawn, list and terminate, each under host admission.

    Keyed by run id. A run lists and terminates only its own processes, and a process of
    another run answers exactly like an absent one. Spawn needs verified sandbox
    isolation; admission is the host's ``admit(run_id, action, subject)``.
    """

    def __init__(
        self,
        admit: Callable[[str, str, str], bool],
        sandbox_verified: Callable[[], bool],
    ) -> None:
        self._admit = admit
        self._verified = sandbox_verified
        self._by_run: dict[str, dict[str, SandboxProcess]] = {}
        self._sequence = 0

    async def spawn(
        self, run_id: str, argv: Sequence[str], *, env: Mapping[str, str] | None = None
    ) -> SandboxProcess:
        if not self._verified():
            raise SandboxProcessRefused("SANDBOX_UNVERIFIED")
        if not argv or not self._admit(run_id, "spawn", str(argv[0])):
            raise SandboxProcessRefused("ADMISSION_DENIED")
        handle = await asyncio.create_subprocess_exec(
            *argv,
            env=dict(env) if env is not None else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._sequence += 1
        process = SandboxProcess(
            f"{run_id}:proc:{self._sequence}", run_id, handle, self._admit
        )
        self._by_run.setdefault(run_id, {})[process.process_id] = process
        return process

    def list(self, run_id: str) -> tuple[SandboxProcess, ...]:
        """The ``SandboxProcessProvider`` read: this run's processes only."""
        return tuple(self._by_run.get(run_id, {}).values())

    async def terminate(self, run_id: str, process_id: str) -> bool:
        process = self._by_run.get(run_id, {}).get(process_id)
        if process is None:
            return False
        await process.kill()
        self._by_run[run_id].pop(process_id, None)
        return True

    async def release(self, run_id: str) -> None:
        """Run end: terminate whatever is still bound, then forget the run."""
        for process in list(self._by_run.pop(run_id, {}).values()):
            if self._admit(run_id, "terminate", process.process_id):
                await process.kill()


class SummaryService:
    """``summary_provider`` backend: engine context compile, then the admitted provider.

    The engine calls no model. ``provider`` is the deployment's admitted summary provider;
    without one the service answers ``None`` and the port raises ``SummaryProviderDisabled``
    with no receipt.
    """

    def __init__(
        self,
        call: RunCall,
        provider: Callable[[Mapping[str, Any], str], Awaitable[Mapping[str, Any]]]
        | None = None,
        *,
        tokenizer: tuple[str, str] = ("cl100k", "1"),
        reserved_output_tokens: int = 512,
    ) -> None:
        self._call = call
        self._provider = provider
        self._tokenizer = tokenizer
        self._reserved = reserved_output_tokens

    async def __call__(
        self, context: Mapping[str, Any], admission_ref: str
    ) -> Mapping[str, Any] | None:
        if self._provider is None:
            return None
        run_ref = str(context.get("run_id") or admission_ref)
        compiled = await self._call(
            COMPILE_OP,
            {
                "request": {
                    "query_text": str(
                        context.get("query") or context.get("subject") or ""
                    ),
                    "budget": {"reserved_output_tokens": self._reserved},
                    "tokenizer_id": self._tokenizer[0],
                    "tokenizer_version": self._tokenizer[1],
                }
            },
            run_ref,
        )
        if compiled.get("outcome") != "answered":
            raise RuntimeError(
                f"context compile refused: {compiled.get('refusal_code')}"
            )
        return await self._provider(
            {"context": dict(context), "compiled": dict(compiled.get("payload") or {})},
            admission_ref,
        )


class StudyContentService:
    """``study_content`` backend: one graph-held source body, by the run principal.

    A source the run principal cannot read, in a graph or tenant it does not own, or at
    another revision answers ``None`` exactly like an absent one.
    """

    def __init__(
        self, call: RunCall, run_ref: Callable[[], str], tenant_of: Callable[[str], str]
    ) -> None:
        self._call = call
        self._run_ref = run_ref
        self._tenant_of = tenant_of

    async def __call__(
        self, tenant_id: str, graph: str, node: str, revision: int
    ) -> bytes | None:
        run_ref = self._run_ref()
        if tenant_id != self._tenant_of(run_ref):
            return None
        answer = await self._call(
            NODE_OP, {"node_id": node, "graph": graph, "revision": revision}, run_ref
        )
        if answer.get("outcome") != "answered":
            return None
        properties = (
            (answer.get("payload") or {}).get("properties")
            or answer.get("payload")
            or {}
        )
        for name in BODY_PROPERTIES:
            value = properties.get(name)
            if isinstance(value, str):
                return value.encode()
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
        return None


class TechniqueMeasureService:
    """``technique_measure`` backend: one ``DecisionLog`` aggregate read per benchmark arm.

    The arm is the decision option; its logged outcomes for the technique question are the
    measure: trials, successes, refusals and the success rate. An arm with no logged
    outcome measures zero trials, never a made-up rate.
    """

    def __init__(
        self,
        call: RunCall,
        run_ref: Callable[[], str],
        tenant_of: Callable[[str], str],
        now_ms: Callable[[], int],
    ) -> None:
        self._call = call
        self._run_ref = run_ref
        self._tenant_of = tenant_of
        self._now_ms = now_ms

    async def __call__(
        self, technique: str, arm: str, config_digest: str
    ) -> Mapping[str, Any]:
        run_ref = self._run_ref()
        answer = await self._call(
            LOG_OP,
            {
                "op": {
                    "op": "aggregate",
                    "request": {
                        "tenant_id": self._tenant_of(run_ref),
                        "question_id": f"technique:{technique}:{config_digest}",
                        "window": {"from_ms": 0, "to_ms": self._now_ms()},
                    },
                }
            },
            run_ref,
        )
        if answer.get("outcome") != "answered":
            raise RuntimeError(f"technique measure refused: {dict(answer)}")
        rows = (answer.get("payload") or {}).get("rows") or ()
        row = next((r for r in rows if r.get("option_id") == arm), {})
        trials = int(row.get("trials") or 0)
        successes = int(row.get("successes") or 0)
        return {
            "trials": trials,
            "successes": successes,
            "refused": int(row.get("refused") or 0),
            "success_rate": successes / trials if trials else 0.0,
        }


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class RunTraceReceipts:
    """``run_trace`` backend: the trace as ``ReceiptNode``s on the run's terminal commit.

    Arbora owns the delegation, fence token and outbox ids; the harness hands plain steps.
    The terminal commit also needs the outcome bundle and run event, which come from
    ``terminal_extension(run_id)``; a host that cannot supply one binds no trace sink.
    """

    def __init__(
        self,
        call: RunCall,
        owner_of: Callable[[str], Any],
        terminal_extension: Callable[[str], Mapping[str, Any]],
        encode: Callable[[Any], bytes],
    ) -> None:
        self._call = call
        self._owner_of = owner_of
        self._terminal = terminal_extension
        self._encode = encode

    def nodes(
        self, run_id: str, steps: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        owner = self._owner_of(run_id)
        claim = owner.claim
        built: list[dict[str, Any]] = []
        for index, step in enumerate(
            [{"kind": "run_trace", "steps": list(steps)}, *steps]
        ):
            body = self._encode(step)
            digest = _digest(body)
            node_id = f"receipt:{run_id}:{index}"
            built.append(
                {
                    "delegation_id": str(claim.get("delegation_id") or owner.task_id),
                    "fence_token": int(claim["fencing_token"]),
                    "kind": "run_trace" if index == 0 else "tool_call",
                    "node_id": node_id,
                    "outbox_id": f"outbox:{node_id}",
                    "payload_digest": digest,
                    "payload_ref": f"sha256:{digest}",
                    "properties_msgpack": body,
                    "run_id": run_id,
                    "work_item_id": owner.work_item_id,
                }
            )
        return built

    async def commit(self, run_id: str, steps: Sequence[Mapping[str, Any]]) -> None:
        owner = self._owner_of(run_id)
        claim = owner.claim
        extension = {
            **self._terminal(run_id),
            "receipt_nodes": self.nodes(run_id, steps),
        }
        answer = await self._call(
            COMMIT_OP,
            {
                "tenant": owner.session.tenant,
                "work_item_id": owner.work_item_id,
                "worker_id": str(claim.get("worker_id") or ""),
                "lease_epoch": int(claim["lease_epoch"]),
                "fencing_token": int(claim["fencing_token"]),
                "idempotency_key": f"run-trace:{run_id}",
                "now_ms": int(claim.get("now_ms") or 0),
                "outcome": "completed",
                "outcome_extension": extension,
            },
            run_id,
        )
        if answer.get("outcome") != "answered":
            raise RuntimeError(
                f"run trace commit refused: {answer.get('refusal_code')}"
            )


class RunVerdictConfirm:
    """``confirm`` backend: a run-keyed verdict over the one plan-confirm flow.

    ``exchange(kind, payload, run_ref)`` is the plan confirmation of ``arbora.plan/confirm``
    under the run principal; its grant store consumes once, so a retry cannot ask a human
    twice and this adapter keeps no verdict of its own. The harness applies ``admit`` and
    denies on everything else, so an unknown kind, a missing verdict or an admit without a
    grant reference never reads as an approval.
    """

    def __init__(
        self,
        exchange: Callable[[str, Mapping[str, Any], str], Awaitable[Mapping[str, Any]]],
    ) -> None:
        self._exchange = exchange

    async def __call__(
        self, kind: str, payload: Mapping[str, Any], run_ref: str
    ) -> Mapping[str, Any]:
        if kind not in CONFIRM_KINDS:
            return {"verdict": "deny", "refusal_code": "confirm_kind_unknown"}
        raw = await self._exchange(kind, payload, run_ref)
        if raw.get("verdict") == "admit" and raw.get("ref"):
            return {"verdict": "admit", "ref": raw["ref"]}
        return {
            "verdict": "deny",
            "refusal_code": str(raw.get("refusal_code") or "confirm_unsigned"),
        }
