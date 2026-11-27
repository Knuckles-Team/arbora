"""Program optimization and revision promotion and rollback (ARBORA-OPS-R860)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import msgpack
import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import require_scopes
from arbora.api.ops import program
from arbora.api.registry import AuditClass, Confirm, Effect, Surface
from arbora.pin_lag import medulla as pin_lag
from arbora.security.audit_event import audit_event
from tests.api._engine_double import EngineDouble, context

_OPS = {op.id: op for op in program.operations()}
_DIGEST = "a" * 64
_SUBJECT = {"kind": "program", "ref": "prog:1"}


@pytest.mark.spec("ARBORA-OPS-R860.1")
def test_params_round_trip_and_reject_unknown_fields() -> None:
    body = {
        "signature_ref": "sig:1",
        "corpus_ref": "corpus:1",
        "optimizer": "bootstrap_few_shot",
        "budget": {"max_candidates": 8, "seed": 7},
    }
    assert program.ProgramOptimizeParams(**body).budget.seed == 7
    with pytest.raises(ValidationError):
        program.ProgramOptimizeParams(**body, extra="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        program.ProgramOptimizeParams(
            **{**body, "budget": {"max_candidates": 129, "seed": 1}}
        )
    with pytest.raises(ValidationError):
        program.RevisionPromoteParams(subject=_SUBJECT, receipt_digest="A" * 64)
    with pytest.raises(ValidationError):
        program.RevisionPromoteParams(
            subject={"kind": "other", "ref": "x"}, receipt_digest=_DIGEST
        )
    promote = program.RevisionPromoteParams(subject=_SUBJECT, receipt_digest=_DIGEST)
    assert promote.model_dump()["subject"] == _SUBJECT
    assert program.RevisionRollbackParams(
        subject=_SUBJECT, expected_champion_ref="rev:2"
    )


@pytest.mark.spec("ARBORA-OPS-R860.3")
def test_registry_lists_three_operations_with_hints() -> None:
    assert sorted(_OPS) == ["program.optimize", "revision.promote", "revision.rollback"]
    optimize = _OPS["program.optimize"]
    assert optimize.effect is Effect.WRITE and optimize.confirm in (None, Confirm.NONE)
    for op_id in ("revision.promote", "revision.rollback"):
        op = _OPS[op_id]
        assert op.effect is Effect.DESTRUCTIVE
        assert op.confirm is Confirm.PLAN
        assert op.audit is AuditClass.IDENTITY_CHAIN
        assert op.scopes == frozenset({"kg:admin"})
        assert {Surface.HTTP, Surface.MCP} <= set(op.surfaces)
    assert set(program.NATIVE_IDEMPOTENCY) == set(_OPS)


@pytest.mark.spec("ARBORA-OPS-R860.3")
async def test_each_operation_reaches_its_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for op in _OPS.values():
        handler = op.binding.handler.rpartition(".")[2]
        assert callable(getattr(program, handler))


@pytest.mark.spec("ARBORA-OPS-R860.2")
async def test_a_pinned_engine_without_the_shape_refuses_naming_it() -> None:
    from medulla.generated import models

    if hasattr(models, "LearningWriteMovePointer"):
        pytest.skip("the pin carries the pointer move")
    with pytest.raises(OperationRefused) as refused:
        await program.handle_revision_rollback(
            context(),
            {"subject": _SUBJECT, "expected_champion_ref": "rev:2"},
            _OPS["revision.rollback"],
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "LearningWriteMovePointer"
    if not hasattr(models, "JobKindProgramOptimize"):
        with pytest.raises(OperationRefused) as optimize:
            await program.handle_program_optimize(
                context(),
                {
                    "signature_ref": "s",
                    "corpus_ref": "c",
                    "optimizer": "x",
                    "budget": {"max_candidates": 1, "seed": 1},
                },
                _OPS["program.optimize"],
            )
        assert optimize.value.details["missing"] == "JobKindProgramOptimize"


class _Engine:
    """The engine's pointer, verdicts and job table behind structural doubles."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, verdict: dict[str, Any]
    ) -> None:
        from medulla.generated import models

        for name in (*program.OPTIMIZE_MODELS, *program.POINTER_MODELS):
            monkeypatch.setattr(models, name, object, raising=False)

        async def graph(_context: Any) -> str:
            return "tenant-content"

        monkeypatch.setattr(program, "caller_dispatch_graph", graph)
        self.champion = "rev:1"
        self.parents = ["rev:0"]
        self.moves = 0
        self.verdict = verdict
        self.double = EngineDouble(monkeypatch)
        self.double.serve("AnalyticsJob", lambda p: {"job_id": "job-9"})
        self.double.serve("Decide", lambda p: self.verdict)
        self.double.serve("DecisionLog", self._log)
        self.double.install(pin_lag)

    def _log(self, p: dict[str, Any]) -> Any:
        write = p["op"]["write"]
        assert write["write"] == "move_pointer"
        move = write["movement"]
        if move["movement"] == "activate":
            before, self.champion = self.champion, move["target"]
            self.parents.append(before)
        else:
            if move["expected_champion_ref"] != self.champion:
                raise EngineRefusal("PROMOTION_CAS_CONFLICT")
            if not self.parents:
                raise EngineRefusal("ROLLBACK_NO_PARENT")
            before, self.champion = self.champion, self.parents.pop()
        self.moves += 1
        return {
            "pointer_event_id": f"evt-{self.moves}",
            "from_ref": before,
            "to_ref": self.champion,
        }


@pytest.mark.spec("ARBORA-OPS-R860", "ARBORA-OPS-R860.2", "ARBORA-OPS-R860.6")
async def test_optimize_promote_rollback_served(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(
        monkeypatch,
        {"verdict": "promote", "champion_ref": "rev:1", "challenger_ref": "rev:2"},
    )
    ctx = context()
    optimized = await program.handle_program_optimize(
        ctx,
        {
            "signature_ref": "sig:1",
            "corpus_ref": "corpus:1",
            "optimizer": "bootstrap_few_shot",
            "budget": {"max_candidates": 4, "seed": 3},
        },
        _OPS["program.optimize"],
    )
    promoted = await program.handle_revision_promote(
        ctx,
        {"subject": _SUBJECT, "receipt_digest": _DIGEST},
        _OPS["revision.promote"],
    )
    rolled = await program.handle_revision_rollback(
        ctx,
        {"subject": _SUBJECT, "expected_champion_ref": "rev:2"},
        _OPS["revision.rollback"],
    )
    assert optimized["value"]["job_id"] == "job-9"
    assert promoted["value"]["to_ref"] == "rev:2"
    assert promoted["value"]["from_ref"] == "rev:1"
    assert rolled["value"]["to_ref"] == "rev:1"
    assert engine.champion == "rev:1"
    submitted = next(p for m, p, _ in engine.double.sent if m == "AnalyticsJob")
    request = msgpack.unpackb(
        submitted["op"]["Submit"]["kind"]["ProgramOptimize"]["request_msgpack"]
    )
    assert request["budget"] == {"max_candidates": 4, "seed": 3}
    assert [m for m, _, _ in engine.double.sent] == [
        "AnalyticsJob",
        "Decide",
        "DecisionLog",
        "DecisionLog",
    ]


@pytest.mark.spec("ARBORA-OPS-R860.2", "ARBORA-OPS-R860.4")
async def test_engine_calls_and_no_local_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(monkeypatch, {"verdict": "hold", "code": "PROMOTION_NO_BASELINE"})
    with pytest.raises(EngineRefusal) as held:
        await program.handle_revision_promote(
            context(),
            {"subject": _SUBJECT, "receipt_digest": _DIGEST},
            _OPS["revision.promote"],
        )
    assert held.value.code == "PROMOTION_NO_BASELINE"
    assert engine.moves == 0
    assert [m for m, _, _ in engine.double.sent] == ["Decide"]


@pytest.mark.spec("ARBORA-OPS-R860.4")
async def test_refusal_codes_and_no_engine_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Engine(monkeypatch, {"verdict": "promote", "challenger_ref": "rev:2"})
    with pytest.raises(EngineRefusal) as stale:
        await program.handle_revision_rollback(
            context(),
            {"subject": _SUBJECT, "expected_champion_ref": "rev:9"},
            _OPS["revision.rollback"],
        )
    assert stale.value.code == "PROMOTION_CAS_CONFLICT"
    assert engine.moves == 0 and engine.champion == "rev:1"
    caller = SimpleNamespace(effective_scopes=frozenset({"kg:read"}))
    for op_id in ("revision.promote", "revision.rollback"):
        refused = require_scopes(_OPS[op_id], caller)
        assert refused is not None and refused.code == "SCOPE_REQUIRED"
    # An unapproved plan is the pipeline's CONFIRMATION_REQUIRED, before any handler.
    assert _OPS["revision.promote"].confirm is Confirm.PLAN


@pytest.mark.spec("ARBORA-OPS-R860.5")
async def test_audit_event_names_principal_revisions_and_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Engine(
        monkeypatch,
        {"verdict": "promote", "challenger_ref": "rev:2", "champion_ref": "rev:1"},
    )
    params = {"subject": _SUBJECT, "receipt_digest": _DIGEST}
    result = await program.handle_revision_promote(
        context(), params, _OPS["revision.promote"]
    )
    caller = SimpleNamespace(
        principal="person:one", tenant="tenant:one", request_id="req-1"
    )
    event = audit_event(_OPS["revision.promote"], params, caller, Surface.HTTP, "ok")
    assert event["principal"] == "person:one"
    # The plan digest is the approval id; the result carries the subject and both revisions.
    assert len(event["plan_digest"]) >= 32
    assert result["value"]["subject"] == _SUBJECT
    assert (result["value"]["from_ref"], result["value"]["to_ref"]) == (
        "rev:1",
        "rev:2",
    )
    assert "corpus" not in repr(event)
