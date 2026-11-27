"""Implementation runs and autonomous deliveries over MCP (ARBORA-OPS-R896.3, R896.4, R896.7, R896.10).

``development.run.start`` and ``development.run.status`` start and observe one
orchestrated implementation run. ``development.delivery.list``, ``.status``, ``.stop``,
``.retry`` and ``.answer`` read and steer autonomous deliveries over the engine delivery
methods, and ``development.delivery.arm`` asks the engine merge-arming decision and calls
the connector's arm tool only on ``arm``. The engine holds every run, delivery, stage and
decision. Arbora admits, stamps the verified caller, forwards and returns the engine answer:
it keeps no state, chooses no task and never merges a proposal itself.

Engine ``main`` serves the delivery methods (``DeliveryOpen``, ``DeliveryStatus``,
``DeliveryRespond``, ...); the pinned release lacks them, so each handler refuses
``UNAVAILABLE`` naming the method until the re-pin. A run is a delivery: ``run.start`` is
``DeliveryOpen`` over the engine's own ``next_unblocked_task`` answer and ``run.status`` is
``DeliveryStatus``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec, PrincipalRule, Verb

READ_SCOPE = "kg:read"
WRITE_SCOPE = "lifecycle:write"

_ID = Field(min_length=1, max_length=256)


class RunStartParams(Params):
    repository: str = _ID
    phase: str = _ID
    task_id: str | None = Field(default=None, min_length=1, max_length=256)


class RunStartResult(Params):
    run_id: str


class RunStatusParams(Params):
    run_id: str = _ID


class RunStatus(Params):
    state: str
    scope_rows: list[str]
    proposals: list[dict[str, Any]]
    evidence: list[str]
    assignments: list[dict[str, Any]]


class DeliveryListParams(Params):
    repository: str | None = Field(default=None, min_length=1, max_length=256)
    stage: str | None = Field(default=None, min_length=1, max_length=64)


class DeliveryListResult(Params):
    items: list[dict[str, Any]]


class DeliveryRef(Params):
    delivery_id: str = _ID


class DeliveryStatusResult(Params):
    stage: str
    mandate: dict[str, Any]
    proposals: list[dict[str, Any]]
    attention: dict[str, Any] | None = None
    ended: str | None = None


class DeliveryEnded(Params):
    delivery_id: str
    outcome: str


class DeliveryRetried(Params):
    delivery_id: str
    run_trace_id: str


class DeliveryAnswerParams(Params):
    delivery_id: str = _ID
    text: str = Field(min_length=1, max_length=2000)


class DeliveryAnswered(Params):
    delivery_id: str
    accepted: bool


class DeliveryArmParams(Params):
    delivery_id: str = _ID
    proposal: str = _ID
    head_sha: str = Field(min_length=7, max_length=64)
    approval_ref: str | None = Field(default=None, min_length=1, max_length=256)


class DeliveryArmResult(Params):
    verdict: Literal["arm", "disarm", "hold"]
    decision_id: str


def _tenant(context: Any) -> str:
    return str(context.caller.tenant)


def _malformed() -> Exception:
    return unavailable("engine result malformed")


def _trace_id(context: Any) -> str:
    """The run trace id of a retry: stable per idempotency key, so a replay reuses it."""
    seed = f"{_tenant(context)}\0{context.idempotency_key}".encode()
    return "run:" + hashlib.sha256(seed).hexdigest()[:32]


async def handle_run_start(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Start one run of a phase as one delivery; the engine picks the unblocked tasks.

    A reference-mode repository refuses before any dispatch. The engine starts no run
    itself (MEDULLA w16 decision): the slice is the engine's own ``next_unblocked_task``
    answer, the engine ``Decide`` ``TaskPlan`` question over that slice is the dispatch
    decision, and ``DeliveryOpen`` records the delivery under that decision's record id.
    Arbora computes no decision id. The run id is the delivery id.
    """
    mode = await engine_send(
        context, "ProjectModeGet", {"request": {"project": params["repository"]}}
    )
    if not isinstance(mode, Mapping) or mode.get("mode") == "reference":
        raise OperationRefused(
            "PROJECT_MODE_REFERENCE", {"repository": params["repository"]}
        )
    picked, digests = await _unblocked_slice(context, params)
    decision = await _dispatch_decision(context, params["phase"], picked)
    answer = await engine_send(
        context,
        "DeliveryOpen",
        {
            "request": {
                "repo": params["repository"],
                "slice_root": params["phase"],
                "scope_rows": picked,
                "dispatch_decision_id": decision,
                "policy_version": str(getattr(context.caller, "policy_version", "")),
                "origin": "requested",
                "run_trace_id": _trace_id(context),
                "evidence_digests": digests,
            }
        },
        idempotent=True,
    )
    try:
        return {"run_id": str(answer["delivery"]["id"])}
    except (KeyError, TypeError):
        raise _malformed() from None


async def _unblocked_slice(
    context: Any, params: Mapping[str, Any]
) -> tuple[list[str], list[str]]:
    """The engine's own ``next_unblocked_task`` rows (and digests), narrowed to ``task_id``."""
    slice_ = await engine_send(
        context,
        "SpecQuestion",
        {
            "question": "next_unblocked_task",
            "subject": params["phase"],
            "graph": await caller_dispatch_graph(context),
        },
    )
    try:
        picked = [str(row["task"]) for row in slice_["rows"]]
        digests = [str(d) for d in slice_["digests"]]
    except (KeyError, TypeError):
        raise _malformed() from None
    if params.get("task_id") is not None:
        picked = [t for t in picked if t == params["task_id"]]
    if not picked:
        raise OperationRefused("NOT_FOUND", {})
    return picked, digests


async def _dispatch_decision(context: Any, phase: str, picked: list[str]) -> str:
    """The record id of the engine ``TaskPlan`` decision over the picked slice."""
    from arbora.api.ops.plans import (
        _feature_schema,
        decide_task_plan,
        engine_task_plan,
    )

    proposal = {
        "tasks": [
            {"task_id": task, "prompt": f"{phase}: {task}", "depends_on": []}
            for task in picked
        ],
        "output_task_id": picked[-1],
        "max_concurrency": len(picked),
        "max_steps": len(picked),
    }
    engine = context.services.get("task_plan_engine") or engine_task_plan(
        context.client
    )
    _plan, record = await decide_task_plan(
        engine,
        proposal,
        tenant=_tenant(context),
        principal=str(context.caller.principal),
        feature_schema=await _feature_schema(context, _tenant(context)),
        question_id=f"arbora.development.run.start:{phase}",
    )
    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise _malformed()
    return record_id


async def handle_run_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's delivery record of the run; a foreign run answers as an absent one."""
    try:
        record = await engine_send(
            context, "DeliveryStatus", {"request": {"delivery_id": params["run_id"]}}
        )
    except EngineRefusal as refused:
        if refused.code == "DELIVERY_NOT_FOUND":
            raise OperationRefused("RUN_NOT_FOUND", {}) from None
        raise
    try:
        assignments = [dict(a) for a in record["assignments"]]
        return {
            "state": str(record["stage"]),
            "scope_rows": list(record["mandate"]["scope_rows"]),
            "proposals": [dict(p) for p in record["proposals"]],
            "evidence": [
                str(e)
                for a in assignments
                for e in ((a.get("report") or {}).get("evidence") or ())
            ],
            "assignments": assignments,
        }
    except (KeyError, TypeError, AttributeError):
        raise _malformed() from None


async def handle_delivery_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request: dict[str, Any] = {}
    if params.get("repository") is not None:
        request["repo"] = params["repository"]
    rows = await engine_send(context, "DeliveryList", {"request": request})
    if not isinstance(rows, list):
        raise _malformed()
    stage = params.get("stage")
    return {
        "items": [
            dict(row)
            for row in rows
            if isinstance(row, Mapping) and stage in (None, row.get("stage"))
        ]
    }


async def handle_delivery_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    answer = await engine_send(
        context, "DeliveryStatus", {"request": {"delivery_id": params["delivery_id"]}}
    )
    try:
        return {
            "stage": str(answer["stage"]),
            "mandate": dict(answer["mandate"]),
            "proposals": [dict(p) for p in answer["proposals"]],
            "attention": answer.get("attention"),
            "ended": answer.get("ended"),
        }
    except (KeyError, TypeError, AttributeError):
        raise _malformed() from None


async def handle_delivery_stop(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    receipt = await engine_send(
        context,
        "DeliveryStop",
        {"request": {"delivery_id": params["delivery_id"]}},
        idempotent=True,
    )
    outcome = receipt.get("outcome") if isinstance(receipt, Mapping) else None
    if outcome != "stopped":
        raise _malformed()
    return {"delivery_id": params["delivery_id"], "outcome": outcome}


async def handle_delivery_retry(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    trace = _trace_id(context)
    await engine_send(
        context,
        "DeliveryRetry",
        {"request": {"delivery_id": params["delivery_id"], "run_trace_id": trace}},
        idempotent=True,
    )
    return {"delivery_id": params["delivery_id"], "run_trace_id": trace}


async def handle_delivery_answer(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The open question's answer as a person's input, stamped with the verified person."""
    request = {"delivery_id": params["delivery_id"], "answer": params["text"]}
    await engine_send(context, "DeliveryRespond", {"request": request}, idempotent=True)
    return {"delivery_id": params["delivery_id"], "accepted": True}


def _arming_request(
    context: Any, params: Mapping[str, Any], feature_schema: Mapping[str, Any]
) -> dict[str, Any]:
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    texts = {
        "delivery_id": params["delivery_id"],
        "proposal": params["proposal"],
        "head_sha": params["head_sha"],
        "approval_ref": params.get("approval_ref") or "",
    }
    return decide_request(
        _tenant(context),
        (
            f"merge-arming:{params['delivery_id']}:{params['proposal']}",
            "merge_arming",
            "irreversible",
        ),
        declared([DeclaredOption(v) for v in ("arm", "disarm", "hold")]),
        feature_schema,
        params=[param(name, "text", value) for name, value in texts.items()],
    )


async def _verified_receipt(context: Any, params: Mapping[str, Any]) -> None:
    """An owner receipt for the exact head commit, or the named refusal (ARBORA-OPS-R863)."""
    ref = params.get("approval_ref")
    if not ref:
        raise OperationRefused("MERGE_APPROVAL_REQUIRED", {})
    receipts = context.services.get("owner_receipts")
    if receipts is None:
        raise unavailable("owner receipts are not composed")
    verdict = await receipts.verify(
        ref, head_sha=params["head_sha"], tenant=_tenant(context)
    )
    if verdict == "head_mismatch":
        raise OperationRefused("MERGE_HEAD_MISMATCH", {})
    if verdict != "ok":
        raise OperationRefused("MERGE_APPROVAL_REQUIRED", {})


async def handle_delivery_arm(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Arm only on the engine's ``arm`` verdict and an owner receipt of the head commit."""
    from arbora.deployment.placement import check_receipt, single_record
    from arbora.pin_lag.medulla import guard

    await _verified_receipt(context, params)
    guard("Decide")
    schema = context.services.get("task_plan_feature_schema")
    if not isinstance(schema, Mapping):
        raise unavailable("no verified feature schema pin")
    request = _arming_request(context, params, schema)
    record = single_record(await engine_send(context, "Decide", {"request": request}))
    # The sealed record must answer this request, for this caller.
    check_receipt(record, request, str(context.caller.principal))
    verdict = _arming_verdict(record)
    connector = context.services.get("delivery_connector")
    if verdict != "hold":
        if connector is None:
            raise unavailable("the code-host connector is not composed")
        tool = "arm_merge" if verdict == "arm" else "disarm_merge"
        await connector.call(
            tool,
            {
                "proposal": params["proposal"],
                "expected_head": params["head_sha"],
                "approval_ref": params["approval_ref"],
            },
        )
    return {"verdict": verdict, "decision_id": str(record.get("record_id", ""))}


def _arming_verdict(record: Mapping[str, Any]) -> str:
    answer = record.get("answer")
    value = answer.get("value") if isinstance(answer, Mapping) else None
    verdict = value.get("verdict") if isinstance(value, Mapping) else None
    if verdict not in ("arm", "disarm", "hold"):
        raise _malformed()
    return str(verdict)


NATIVE_IDEMPOTENCY: dict[str, str] = {
    "development.run.start": "medulla:DeliveryOpen",
    "development.delivery.stop": "medulla:DeliveryStop",
    "development.delivery.retry": "medulla:DeliveryRetry",
    "development.delivery.answer": "medulla:DeliveryRespond",
}


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.development_delivery"
    return (
        build_write_op(
            op_id="development.run.start",
            summary="Start one orchestrated implementation run of a phase; the engine picks the tasks",
            examples=("start the implementation run for this phase",),
            params=RunStartParams,
            result=RunStartResult,
            handler=f"{module}.handle_run_start",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="development.run.status",
            summary="Read one implementation run: state, task lines, evidence digests and gate verdicts",
            examples=("how is this implementation run going",),
            params=RunStatusParams,
            result=RunStatus,
            handler=f"{module}.handle_run_status",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="development.delivery.list",
            summary="List autonomous deliveries by repository and stage",
            examples=("list the deliveries waiting on a person",),
            params=DeliveryListParams,
            result=DeliveryListResult,
            handler=f"{module}.handle_delivery_list",
            scope=READ_SCOPE,
            verb=Verb.FIND,
        ),
        build_read_op(
            op_id="development.delivery.status",
            summary="Read one delivery: stage, mandate, proposals and the open question",
            examples=("what is the state of this delivery",),
            params=DeliveryRef,
            result=DeliveryStatusResult,
            handler=f"{module}.handle_delivery_status",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="development.delivery.stop",
            summary="End a delivery as stopped; only a person may",
            examples=("stop this delivery",),
            params=DeliveryRef,
            result=DeliveryEnded,
            handler=f"{module}.handle_delivery_stop",
            scope=WRITE_SCOPE,
            principals=PrincipalRule.HUMAN,
        ),
        build_write_op(
            op_id="development.delivery.retry",
            summary="Start a new assignment for a blocked or failed delivery",
            examples=("retry this delivery",),
            params=DeliveryRef,
            result=DeliveryRetried,
            handler=f"{module}.handle_delivery_retry",
            scope=WRITE_SCOPE,
        ),
        build_write_op(
            op_id="development.delivery.answer",
            summary="Answer the open question of a delivery as a person's input",
            examples=("answer the delivery's question",),
            params=DeliveryAnswerParams,
            result=DeliveryAnswered,
            handler=f"{module}.handle_delivery_answer",
            scope=WRITE_SCOPE,
            verb=Verb.WRITE,
            principals=PrincipalRule.HUMAN,
        ),
        build_write_op(
            op_id="development.delivery.arm",
            summary="Arm or disarm the merge of a delivery's proposal on the engine verdict and an owner receipt of its head commit",
            examples=("arm the merge for this proposal",),
            params=DeliveryArmParams,
            result=DeliveryArmResult,
            handler=f"{module}.handle_delivery_arm",
            scope=WRITE_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_delivery_answer",
    "handle_delivery_arm",
    "handle_delivery_list",
    "handle_delivery_retry",
    "handle_delivery_status",
    "handle_delivery_stop",
    "handle_run_start",
    "handle_run_status",
    "operations",
    "specs",
]
