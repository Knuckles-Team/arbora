"""The meristem host ports Arbora binds, each over one backend seam it is handed.

Every adapter is a thin translation between a harness port and a backend callable that
the host composition root supplies; none holds state or decides anything. The result
types come from ``arbora.pin_lag.meristem`` so a pinned release without them refuses
``UNAVAILABLE`` naming the missing symbol (PIN-LAG: the guard goes after the re-pin).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arbora.deployment.placement import decide_params
from arbora.pin_lag.meristem import PinLagUnavailable, changeset_types, port_types

#: ``(operation id, params, run ref[, plan_ref])`` to the engine result as plain data. An
#: answered result carries the pipeline ``code`` and ``details``: a plan-gated operation
#: called without a ``plan_ref`` answers ``CONFIRMATION_REQUIRED`` with the plan reference.
logger = logging.getLogger(__name__)

RunCall = Callable[..., Awaitable[Mapping[str, Any]]]
PLAN_REQUIRED = "CONFIRMATION_REQUIRED"

DECIDE_OP = "eg.query.Decide"
#: The harness's control-step questions, as the engine's snake_case ``QuestionKind``.
CONTROL_QUESTION_KINDS: Mapping[str, str] = {
    "BranchSelect": "branch_select",
    "DecisionTableEval": "decision_table_eval",
}
#: ``(tenant id, question id) -> the pinned feature schema`` or ``None`` when none is pinned.
SchemaOf = Callable[[str, str], Mapping[str, Any] | None]
TenantOf = Callable[[str], str]
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


def _wire_kind(kind: str) -> str:
    """The engine's snake_case ``QuestionKind`` for a harness question."""
    if kind in CONTROL_QUESTION_KINDS:
        return CONTROL_QUESTION_KINDS[kind]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", kind).lower()


def _typed_param(name: str, value: Any) -> dict[str, Any]:
    """One ``TypedParam``; a float or a nested value has no typed form and is refused."""
    from medulla.decision_client import param

    if isinstance(value, bool):
        return param(name, "bool", value)
    if isinstance(value, int):
        return param(name, "int", value)
    if isinstance(value, str):
        return param(name, "iri" if name == "decision_iri" else "text", value)
    raise port_types().EngineDecisionRefused("decide_param_untyped")


def _question_params(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The payload as typed params: scalars by name, ``inputs`` as ``input.<name>``."""
    typed: list[dict[str, Any]] = []
    for name, value in payload.items():
        if name == "inputs" and isinstance(value, Mapping):
            typed += [_typed_param(f"input.{k}", v) for k, v in value.items()]
        else:
            typed.append(_typed_param(str(name), value))
    return typed


def _dmn_value(value: Any) -> dict[str, Any]:
    """One input as the engine's ``DmnValue``; a float has no fixed-point form and is refused."""
    from decimal import Decimal

    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, int):
        return {"type": "integer", "value": value}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, Decimal):
        sign, digits, exponent = value.as_tuple()
        if not isinstance(exponent, int) or exponent > 0:
            raise port_types().EngineDecisionRefused("decide_param_untyped")
        units = int("".join(map(str, digits))) * (-1 if sign else 1)
        return {"type": "decimal", "value": {"units": units, "scale": -exponent}}
    raise port_types().EngineDecisionRefused("decide_param_untyped")


def _control_params(payload: Mapping[str, Any], graph: str) -> list[dict[str, Any]]:
    """The two text params the control-step families read: ``graph`` and ``process_decision``."""
    import json

    from medulla.decision_client import param

    decision = {
        name: (
            {k: _dmn_value(v) for k, v in value.items()}
            if name == "inputs" and isinstance(value, Mapping)
            else value
        )
        for name, value in payload.items()
    }
    text = json.dumps(decision, sort_keys=True, separators=(",", ":"))
    return [param("graph", "text", graph), param("process_decision", "text", text)]


def decide_request(
    kind: str,
    payload: Mapping[str, Any],
    *,
    tenant: str,
    schema_of: SchemaOf | None,
    graph: str = "",
) -> dict[str, Any]:
    """The full ``DecideRequest`` the host sends for one harness question (PIN-LAG).

    The harness port carries only ``(kind, payload)``. The host supplies the rest: the
    run's verified tenant, a question id, the pinned feature schema and the candidate set.
    A pinned engine that does not list the question kind refuses ``UNAVAILABLE`` naming
    it; an unpinned schema or an untyped payload value refuses, and nothing is sent.
    """
    from medulla.decision_client import decide_request as build

    from arbora.pin_lag.medulla import require_question_kind

    wire = _wire_kind(kind)
    require_question_kind(wire)
    question_id = f"process.{wire}"
    schema = schema_of(tenant, question_id) if schema_of is not None else None
    if not tenant or schema is None:
        raise port_types().EngineDecisionRefused("decide_request_incomplete")
    candidates, params = _candidates_and_params(wire, payload, graph)
    return build(
        tenant, (question_id, wire, "ordinary"), candidates, schema, params=params
    )


def _candidates_and_params(
    wire: str, payload: Mapping[str, Any], graph: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A control step declares the one option ``decision``; any other question its typed payload."""
    if wire not in CONTROL_QUESTION_KINDS.values():
        return {"source": "declared", "options": []}, _question_params(payload)
    if not graph:
        raise port_types().EngineDecisionRefused("decide_request_incomplete")
    from medulla.decision_client import DeclaredOption, declared

    return declared([DeclaredOption("decision")]), _control_params(payload, graph)


class EngineDecisionAdapter:
    """``EngineDecisionPort`` over registered engine operations under the run principal."""

    def __init__(
        self,
        call: RunCall,
        method_op: Callable[[str], str | None] = lambda _: None,
        *,
        tenant_of: TenantOf = lambda _: "",
        schema_of: SchemaOf | None = None,
        graph_of: TenantOf = lambda _: "",
    ) -> None:
        self._call = call
        self._method_op = method_op
        self._tenant_of = tenant_of
        self._schema_of = schema_of
        self._graph_of = graph_of

    async def decide(
        self, kind: str, params: Mapping[str, Any], *, run_ref: str
    ) -> Any:
        request = decide_request(
            kind,
            params,
            tenant=self._tenant_of(run_ref),
            schema_of=self._schema_of,
            graph=self._graph_of(run_ref),
        )
        return _answer(await self._call(DECIDE_OP, decide_params(request), run_ref))

    async def assemble(self, request: Mapping[str, Any], *, run_ref: str) -> Any:
        return _answer(await self._call(ASSEMBLE_OP, dict(request), run_ref))

    async def log(self, op: str, body: Mapping[str, Any], *, run_ref: str) -> Any:
        """One call and one atomic engine write: the body travels with the op."""
        return _answer(
            await self._call(LOG_OP, {"op": {"op": op.lower(), **body}}, run_ref)
        )

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
    *,
    tenant_of: TenantOf = lambda _: "",
    schema_of: SchemaOf | None = None,
    graph_of: TenantOf = lambda _: "",
) -> Callable[[str, Mapping[str, Any], str], Awaitable[Any]]:
    """``DecisionPort``: a control-step question answered by the engine decision."""

    async def decide(question: str, payload: Mapping[str, Any], task_id: str) -> Any:
        request = decide_request(
            question,
            payload,
            tenant=tenant_of(task_id),
            schema_of=schema_of,
            graph=graph_of(task_id),
        )
        raw = verdict_fields(await call(DECIDE_OP, decide_params(request), task_id))
        return port_types().DecisionVerdict(
            record_id=str(raw.get("record_id") or ""),
            taken_task_ids=tuple(raw.get("taken_task_ids") or ()),
            outputs=raw.get("outputs"),
            abstention=raw.get("abstention"),
        )

    return decide


def _abstention(outcome: Mapping[str, Any]) -> str:
    """The registered refusal code of an abstained outcome."""
    reasons = outcome.get("reasons")
    codes = [
        str(reason["code"])
        for reason in (reasons if isinstance(reasons, list) else [])
        if isinstance(reason, Mapping) and reason.get("code")
    ]
    return codes[0] if codes else "DECISION_MALFORMED"


def _option_ids(outcome: Mapping[str, Any]) -> list[str] | None:
    """The scored option ids of an advisory outcome, else ``None``."""
    scores = outcome.get("scores")
    if outcome.get("outcome") != "advisory" or not isinstance(scores, list):
        return None
    return [
        str(item["option_id"])
        for item in scores
        if isinstance(item, Mapping) and "option_id" in item
    ]


def _table_outputs(ids: Sequence[str]) -> Any:
    """The parsed ``outputs:<json>`` option, ``None`` when absent."""
    import json

    for option in ids:
        if option.startswith("outputs:"):
            return json.loads(option.removeprefix("outputs:"))
    return None


def _advisory_fields(record: Mapping[str, Any], ids: Sequence[str]) -> dict[str, Any]:
    try:
        outputs = _table_outputs(ids)
    except ValueError:
        return {"abstention": "DECISION_MALFORMED"}
    return {
        "record_id": record.get("record_id"),
        "taken_task_ids": [
            i.removeprefix("taken:") for i in ids if i.startswith("taken:")
        ],
        "outputs": outputs,
    }


def _only_record(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    records = (raw.get("payload") or {}).get("records")
    if not isinstance(records, list) or len(records) != 1:
        return None
    return records[0] if isinstance(records[0], Mapping) else {}


def verdict_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The verdict an answered ``Decide`` carries, from the ``RunCall`` envelope.

    A control-step answer is an advisory whose scored option ids are ``taken:<task id>``
    and ``default:<bool>`` (a branch) or ``rule:<id>`` and ``outputs:<json>`` (a table). An
    abstention is the engine's registered refusal code. A refused call, or an answer that
    is not exactly one record, abstains with its code: the harness then fails the step and
    applies nothing.
    """
    if raw.get("outcome") != "answered":
        return {"abstention": str(raw.get("refusal_code") or "UNAVAILABLE")}
    record = _only_record(raw)
    outcome = None if record is None else record.get("outcome")
    if record is None or not isinstance(outcome, Mapping):
        return {"abstention": "DECISION_MALFORMED"}
    if outcome.get("outcome") == "abstained":
        return {"abstention": _abstention(outcome)}
    ids = _option_ids(outcome)
    if ids is None:
        return {"abstention": "DECISION_MALFORMED"}
    return _advisory_fields(record, ids)


def approval_waiter(
    decided: Callable[[str, str, str, str], Awaitable[Mapping[str, Any]]],
    wait: Callable[[float], Awaitable[None]],
) -> Callable[[str, str, str, int, str], Awaitable[Any]]:
    """``ApprovalWaiter``: the decision races an injected clock; the loser is cancelled.

    ``plan_ref`` is the received plan's reference, which keys the inbox item.
    """

    async def wait_for_approval(
        run_id: str, task_id: str, approver_role: str, timeout_s: int, plan_ref: str
    ) -> Any:
        types = port_types()
        answer = asyncio.ensure_future(
            decided(run_id, task_id, approver_role, plan_ref)
        )
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
        except Exception:  # injected confirm owner: fail closed to deny
            logger.warning("confirmation owner failed; denying", exc_info=True)
            return types.ConfirmVerdict(
                verdict="deny", refusal_code="confirm_unavailable"
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


COMMIT_OP = "source.commit"
LAND_OP = "source.land"
FILE_MODE = 0o100644
DIRECTORY_MODE_PREFIX = "04"


@dataclass(frozen=True, slots=True)
class SourceTarget:
    """The repository, branch and declared prefixes one run may change."""

    repository: str
    branch: str
    prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChangesetSeams:
    """What the host supplies for the changeset ports; an absent one leaves them unbound.

    ``target`` answers ``None`` for a run that holds no source grant. ``source_client``
    is the served source client of that run (its reads are the ``source.*`` operations).
    ``stage_blob`` stores content and answers ``{digest, size}`` for ``source.commit``.
    ``is_sandbox`` says whether a workspace backend is the isolated sandbox backend.
    """

    target: Callable[[str], SourceTarget | None]
    source_client: Callable[[str], Any]
    stage_blob: Callable[[str, bytes], Awaitable[Mapping[str, Any]]]
    is_sandbox: Callable[[Any], bool]


def _empty(root: Path) -> None:
    """Remove everything under ``root`` without following a link out of it."""
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


class ChangesetSubmitter:
    """``ChangesetSubmitter`` for one run: reads, materializes and submits through the door.

    ``submit`` invokes ``source.commit`` then ``source.land`` as the run principal; landing
    stays the owner side's decision, so a refused land still reports the submission. A
    ``SOURCE_CONFLICT`` answer returns its paths unchanged for the harness rebase. Any other
    refusal returns a conflict with no paths, which the harness reports as refused.
    """

    def __init__(
        self, call: RunCall, seams: ChangesetSeams, run_ref: str, target: SourceTarget
    ) -> None:
        self._call, self._seams = call, seams
        self._run_ref, self._target = run_ref, target
        self._materialized: set[str] = set()

    async def read_head(self) -> str:
        from scion_sdk.source.client import TreeRequest

        page = await self._seams.source_client(self._run_ref).tree(
            TreeRequest(
                repository_id=self._target.repository,
                ref=self._target.branch,
                path_prefix=self._target.prefixes[0],
                page_size=1,
            )
        )
        return str(page.provenance.revision)

    async def materialize(self, head: str, workspace: Any) -> None:
        """Replace the workspace with exactly the snapshot at ``head``."""
        try:
            from scion_sdk.source.mount import SourceMount
        except ImportError:
            raise PinLagUnavailable("scion_sdk.source.mount.SourceMount") from None
        root = Path(workspace.backend.root)
        _empty(root)
        mount = await SourceMount.start(
            self._seams.source_client(self._run_ref),
            root=root,
            repository_id=self._target.repository,
            ref=head,
            branch=self._target.branch,
            prefixes=self._target.prefixes,
        )
        self._materialized = set()
        for prefix in self._target.prefixes:
            for entry in await mount.listdir(prefix):
                if entry.mode.startswith(DIRECTORY_MODE_PREFIX):
                    continue
                await mount.read(entry.path)
                self._materialized.add(entry.path)

    async def _confirmed(
        self, op_id: str, params: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Run one plan-gated source operation: preview, then execute under its plan ref.

        The pipeline mints the plan reference and the approver gate decides whether it
        runs (ARBORA-OPS-R918); an operation that is not plan-gated answers directly.
        """
        preview = await self._call(op_id, params, self._run_ref)
        details = preview.get("details") or {}
        plan_ref = details.get("plan_ref")
        if (
            preview.get("outcome") != "answered"
            or preview.get("code") != PLAN_REQUIRED
            or not isinstance(plan_ref, str)
            or not plan_ref
        ):
            return preview
        return await self._call(op_id, params, self._run_ref, plan_ref=plan_ref)

    async def _path(self, path: str, content: str) -> dict[str, Any]:
        data = content.encode("utf-8")
        blob = await self._seams.stage_blob(self._run_ref, data)
        return {
            "path": path,
            "op": "modify" if path in self._materialized else "add",
            "mode": FILE_MODE,
            "blob": {"digest": str(blob["digest"]), "size": int(blob["size"])},
        }

    async def submit(self, pending: Any) -> Any:
        types = changeset_types()
        paths = [await self._path(path, text) for path, text in pending.files]
        committed = await self._confirmed(
            COMMIT_OP,
            {
                "repository": self._target.repository,
                "branch": self._target.branch,
                "message": f"changeset of run {pending.run_ref}",
                "paths": paths,
                "base_head": pending.base_head,
                "run_trace_id": pending.run_ref,
            },
        )
        if committed.get("outcome") != "answered":
            conflict = committed.get("refusal_code") == "SOURCE_CONFLICT"
            found = (committed.get("payload") or {}).get("paths") or ()
            return types.SourceConflict(
                paths=tuple(str(path) for path in found) if conflict else ()
            )
        receipt = ((committed.get("payload") or {}).get("value") or {}).get(
            "changeset"
        ) or {}
        changeset_id = str(receipt.get("id") or "")
        if not changeset_id:
            return types.SourceConflict(paths=())
        await self._confirmed(
            LAND_OP,
            {
                "repository": self._target.repository,
                "branch": self._target.branch,
                "changeset": changeset_id,
            },
        )
        return types.Submitted(changeset_id=changeset_id)


class SandboxGateRunner:
    """``SandboxGateRunner``: edits, reads and gates on one workspace, only in the sandbox."""

    def __init__(self, seams: ChangesetSeams) -> None:
        self._seams = seams

    async def apply_edits(self, workspace: Any, edits: tuple[Any, ...]) -> bool:
        """Validate the whole edit set without writing, then write it."""
        from arbora.pin_lag.meristem import apply_edits

        if not self._seams.is_sandbox(workspace.backend):
            return False
        root = Path(workspace.backend.root)
        if not apply_edits(list(edits), root, dry_run=True).ok:
            return False
        return bool(apply_edits(list(edits), root).ok)

    async def read_files(
        self, workspace: Any, paths: tuple[str, ...]
    ) -> tuple[tuple[str, str], ...]:
        if not self._seams.is_sandbox(workspace.backend):
            raise OSError("SANDBOX_UNAVAILABLE")
        root = Path(workspace.backend.root).resolve()
        found: list[tuple[str, str]] = []
        for path in paths:
            resolved = (root / path).resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise OSError("path escapes the workspace")
            found.append((path, resolved.read_text(encoding="utf-8")))
        return tuple(found)

    async def run_gate(self, workspace: Any, command: str) -> Any:
        from meristem.runtime.events import CmdOutputObservation, CmdRunAction

        if not self._seams.is_sandbox(workspace.backend):
            return CmdOutputObservation(exit_code=126, stderr="SANDBOX_UNAVAILABLE")
        observation = await workspace.act(CmdRunAction(command=command))
        if isinstance(observation, CmdOutputObservation):
            return observation
        return CmdOutputObservation(exit_code=1, stderr="gate produced no exit status")


def changeset_submitter_factory(
    call: RunCall, seams: ChangesetSeams, sandbox_verified: Callable[[], bool]
) -> Callable[[str], Any | None]:
    """``run_id`` to the run's submitter; unverified isolation or no grant binds none."""

    def for_run(run_id: str) -> Any | None:
        target = seams.target(run_id) if sandbox_verified() else None
        if target is None:
            return None
        return ChangesetSubmitter(call, seams, run_id, target)

    return for_run


def gate_runner_factory(
    seams: ChangesetSeams, sandbox_verified: Callable[[], bool]
) -> Callable[[str], Any | None]:
    """``run_id`` to the run's sandbox gate runner; unverified isolation binds none."""

    def for_run(run_id: str) -> Any | None:
        if not sandbox_verified() or seams.target(run_id) is None:
            return None
        return SandboxGateRunner(seams)

    return for_run


class HostPortsBinding:
    """The outcome of binding the lane ports: the record, or the refusal naming the gap."""

    __slots__ = ("ports", "refusal")

    def __init__(self, ports: Any | None, refusal: Any | None) -> None:
        self.ports = ports
        self.refusal = refusal


class HostPortsSlot:
    """The host's one ``HostPorts`` binding, set once the composition root has built it.

    It lives on ``HostContext.runs`` and owns its lock. A run entry point reads
    :meth:`current` when it starts a run, so a run that starts before the host binds sees
    the harness's empty record (every binding port fails closed) and a refused record
    raises its refusal.
    """

    __slots__ = ("_binding", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._binding: HostPortsBinding | None = None

    def bind(self, binding: HostPortsBinding) -> None:
        """Hold ``binding`` as the host's record, or the refusal naming the gap."""
        with self._lock:
            self._binding = binding

    @property
    def binding(self) -> HostPortsBinding | None:
        """The bound ``HostPortsBinding``, or ``None`` before the host binds."""
        with self._lock:
            return self._binding

    def current(self) -> Any:
        """The bound ``HostPorts``; the harness's empty record before the host binds."""
        binding = self.binding
        if binding is None:
            from arbora.pin_lag.meristem import host_ports_type

            return host_ports_type()()
        if binding.refusal is not None:
            raise binding.refusal
        return binding.ports


def bind_host_ports(**bound: Any) -> HostPortsBinding:
    """Build the ``HostPorts`` record, or return the ``UNAVAILABLE`` that names the gap."""
    from arbora.pin_lag.meristem import PinLagUnavailable, build_host_ports

    try:
        return HostPortsBinding(build_host_ports(**bound), None)
    except PinLagUnavailable as refusal:
        return HostPortsBinding(None, refusal)
