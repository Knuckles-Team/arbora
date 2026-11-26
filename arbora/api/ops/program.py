"""Program optimization and revision promotion and rollback (ARBORA-OPS-R860).

One family serves every revision subject: ``decision_head``, ``program``, ``knob_set``
and ``model_policy``. ``program.optimize`` submits the engine optimize job and returns its
job id. ``revision.promote`` asks the engine ``Decide`` for a ``Promotion`` verdict and,
only on a promote verdict under an approved plan, commits the champion pointer move
through ``DecisionLog``. ``revision.rollback`` pins the expected champion and moves the
pointer back to its parent. Arbora computes no score, no verdict and no rollback rule: a
hold verdict, a stale champion or any engine refusal reaches the caller with the engine's
own code and no pointer move.

The job kind and the pointer-move shapes are engine ``main`` shapes the pinned release
lacks, so every operation refuses ``UNAVAILABLE`` naming the missing shape until the
re-pin.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

import msgpack
from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.ops._common import Params, build_write_op
from arbora.api.registry import AuditClass, Confirm, Effect, OpSpec, Verb
from arbora.pin_lag.medulla import guard, send_main

OPTIMIZE_SCOPE = "jobs:write"
REVISION_SCOPE = "kg:admin"
OPTIMIZE_MODELS: tuple[str, ...] = ("JobKindProgramOptimize",)
POINTER_MODELS: tuple[str, ...] = ("LearningWriteMovePointer",)
_DIGEST = r"^[0-9a-f]{64}$"
SubjectKind = Literal["decision_head", "program", "knob_set", "model_policy"]


class Subject(Params):
    kind: SubjectKind
    ref: str = Field(min_length=1, max_length=512)


class OptimizeBudget(Params):
    max_candidates: int = Field(ge=1, le=128)
    seed: int


class ProgramOptimizeParams(Params):
    signature_ref: str = Field(min_length=1, max_length=512)
    corpus_ref: str = Field(min_length=1, max_length=512)
    #: The engine owns the closed optimizer list and refuses an unknown name.
    optimizer: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    budget: OptimizeBudget


class RevisionPromoteParams(Params):
    subject: Subject
    receipt_digest: str = Field(pattern=_DIGEST)


class RevisionRollbackParams(Params):
    subject: Subject
    expected_champion_ref: str = Field(min_length=1, max_length=512)


class ProgramResult(Params):
    value: dict[str, Any]


def _answer(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _movement(
    tenant: str, subject: Mapping[str, Any], movement: dict[str, Any]
) -> dict[str, Any]:
    return {
        "op": {
            "op": "learn",
            "tenant_id": tenant,
            "write": {
                "write": "move_pointer",
                "pointer": {"pointer": "champion", "subject": dict(subject)},
                "movement": movement,
            },
        }
    }


async def handle_program_optimize(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Submit the optimize job; the engine runs it and owns every result."""
    guard("AnalyticsJob", models=OPTIMIZE_MODELS)
    request = {
        "signature_ref": params["signature_ref"],
        "corpus_ref": params["corpus_ref"],
        "optimizer": params["optimizer"],
        "budget": dict(params["budget"]),
    }
    submit = {
        "graph": await caller_dispatch_graph(context),
        "kind": {"ProgramOptimize": {"request_msgpack": msgpack.packb(request)}},
        "actor": str(context.caller.principal),
        "purpose": "program.optimize",
    }
    answer = _answer(
        await send_main(
            context,
            "AnalyticsJob",
            {"op": {"Submit": submit}},
            idempotent=True,
            models=OPTIMIZE_MODELS,
        )
    )
    return {"value": {"job_id": answer.get("job_id")}}


async def handle_revision_promote(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Promote after the engine verdict; a hold verdict never reaches the pointer."""
    subject = dict(params["subject"])
    tenant = str(context.caller.tenant)
    verdict = _answer(
        await send_main(
            context,
            "Decide",
            {
                "request": {
                    "tenant_id": tenant,
                    "question": {
                        "kind": "Promotion",
                        "mode": "promote",
                        "subject": subject,
                        "receipt_digest": params["receipt_digest"],
                    },
                }
            },
        )
    )
    if verdict.get("verdict") != "promote":
        raise EngineRefusal(str(verdict.get("code") or "PROMOTION_UNEVALUATED"))
    target = str(verdict["challenger_ref"])
    moved = _answer(
        await send_main(
            context,
            "DecisionLog",
            _movement(
                tenant,
                subject,
                {
                    "movement": "activate",
                    "target": target,
                    "receipt_digest": params["receipt_digest"],
                },
            ),
            idempotent=True,
            models=POINTER_MODELS,
        )
    )
    return {
        "value": {
            "subject": subject,
            "pointer_event_id": moved.get("pointer_event_id"),
            "from_ref": moved.get("from_ref", verdict.get("champion_ref")),
            "to_ref": moved.get("to_ref", target),
        }
    }


async def handle_revision_rollback(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Roll back to the parent; the engine refuses a stale champion with ``PROMOTION_CAS_CONFLICT``."""
    subject = dict(params["subject"])
    moved = _answer(
        await send_main(
            context,
            "DecisionLog",
            _movement(
                str(context.caller.tenant),
                subject,
                {
                    "movement": "rollback",
                    "expected_champion_ref": params["expected_champion_ref"],
                },
            ),
            idempotent=True,
            models=POINTER_MODELS,
        )
    )
    return {
        "value": {
            "subject": subject,
            "pointer_event_id": moved.get("pointer_event_id"),
            "from_ref": moved.get("from_ref", params["expected_champion_ref"]),
            "to_ref": moved.get("to_ref"),
        }
    }


#: Writes replay under the engine method that holds the record, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "program.optimize": "medulla:AnalyticsJob",
    "revision.promote": "medulla:DecisionLog",
    "revision.rollback": "medulla:DecisionLog",
}


def operations() -> tuple[OpSpec, ...]:
    optimize = build_write_op(
        verb=Verb.ACT,
        op_id="program.optimize",
        summary="Optimize one program against a corpus",
        examples=("optimize this program",),
        params=ProgramOptimizeParams,
        result=ProgramResult,
        handler=f"{__name__}.handle_program_optimize",
        scope=OPTIMIZE_SCOPE,
    )

    def revision(
        op_id: str, summary: str, example: str, params: type[Params]
    ) -> OpSpec:
        return build_write_op(
            verb=Verb.ACT,
            effect=Effect.DESTRUCTIVE,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=ProgramResult,
            handler=f"{__name__}.handle_{op_id.replace('.', '_')}",
            scope=REVISION_SCOPE,
            confirm=Confirm.PLAN,
        ).model_copy(update={"audit": AuditClass.IDENTITY_CHAIN})

    return (
        optimize,
        revision(
            "revision.promote",
            "Promote a challenger revision to champion under an approved plan",
            "promote this revision",
            RevisionPromoteParams,
        ),
        revision(
            "revision.rollback",
            "Roll a champion back to its parent revision under an approved plan",
            "roll this revision back",
            RevisionRollbackParams,
        ),
    )


specs = operations
