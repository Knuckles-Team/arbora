"""Harness lesson query: registration, refusal, engine ranking order
(ARBORA-OPS-R068.2).

Wiring tests only: the served round trip is pending the engine release
carrying the improvement-library query.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import harness_lessons
from arbora.api.ops.harness_lessons import (
    UNAVAILABLE_LESSON_RETRIEVAL,
    HarnessLessonHit,
    LessonRetrievalRead,
    operations,
)
from arbora.api.ops.registry_factory import get_registry
from tests.api._ops_support import harness_context

SERVICE = "harness_lesson_retriever"


class _LessonRetriever:
    def __init__(self, read: LessonRetrievalRead | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.read = read or UNAVAILABLE_LESSON_RETRIEVAL

    async def retrieve(self, **kwargs: object) -> LessonRetrievalRead:
        self.calls.append(kwargs)
        return self.read


def _hit(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "task_class": "task:retrieval",
        "deficiency": "stale-context",
        "situation": "long task, drifting context",
        "changed_component_kind": "memory",
        "what_changed": "prune context before retry",
        "applicability": "tasks over ten steps",
        "confidence": 0.5,
        "evidence_run_ids": ("run-a", "run-b"),
        "supersedes_id": None,
    }
    values.update(changes)
    return values


def _read(
    entries: list[dict[str, object]] | None = None, **changes: object
) -> LessonRetrievalRead:
    values: dict[str, object] = {
        "availability": "available",
        "hits": [HarnessLessonHit.model_validate(e) for e in entries or []],
        "abstained": False,
    }
    values.update(changes)
    return LessonRetrievalRead.model_validate(values)


def _op():
    (op,) = operations()
    return op


def test_lesson_op_is_registered_with_read_authority() -> None:
    op = _op()
    assert op.id == "harness.lessons.retrieve"
    assert op.verb.value == "ask"
    assert op.scopes == frozenset({"kg:read"})
    assert op.effect.value == "read"
    assert op.idempotency.value == "none"
    assert op.binding.handler == (
        "arbora.api.ops.harness_lessons.handle_harness_lessons_retrieve"
    )
    assert "harness.lessons.retrieve" in {op.id for op in get_registry()}


async def test_absent_retriever_refuses_unavailable(op_by_id) -> None:
    context = harness_context(services={})
    with pytest.raises(OperationRefused) as excinfo:
        await harness_lessons.handle_harness_lessons_retrieve(
            context, {"task_class": "t", "deficiency": "d"}, _op()
        )
    assert excinfo.value.code == "UNAVAILABLE"
    assert excinfo.value.details["reason"] == "HARNESS_READER_UNAVAILABLE"
    assert excinfo.value.details["provenance"] == (
        f"harness.admission:harness.lessons.retrieve:{SERVICE}-uncomposed"
    )


async def test_denied_scope_refuses_before_the_retriever(op_by_id) -> None:
    retriever = _LessonRetriever()
    context = harness_context(services={SERVICE: retriever}, scopes=())
    with pytest.raises(OperationRefused) as excinfo:
        await harness_lessons.handle_harness_lessons_retrieve(
            context, {"task_class": "t", "deficiency": "d"}, _op()
        )
    assert excinfo.value.code == "SCOPE_REQUIRED"
    assert excinfo.value.details["missing_scopes"] == ["kg:read"]
    assert retriever.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"deficiency": "d"},
        {"task_class": "t"},
        {"task_class": "", "deficiency": "d"},
        {"task_class": "t", "deficiency": "d", "tenant": "x"},
        {"task_class": "t", "deficiency": "d", "trajectory_features": {"f": "hi"}},
    ],
)
async def test_malformed_params_never_reach_the_retriever(op_by_id, params) -> None:
    retriever = _LessonRetriever()
    context = harness_context(services={SERVICE: retriever})
    with pytest.raises(ValidationError):
        await harness_lessons.handle_harness_lessons_retrieve(context, params, _op())
    assert retriever.calls == []


async def test_answer_order_follows_the_engine_ranking(op_by_id) -> None:
    """Arbora preserves the engine confidence ranking; it ranks nothing."""
    retriever = _LessonRetriever(
        read=_read(
            [
                _hit(confidence=0.9, what_changed="first"),
                _hit(confidence=0.5, what_changed="second"),
                _hit(confidence=0.1, what_changed="third"),
            ]
        )
    )
    context = harness_context(services={SERVICE: retriever})
    result = await harness_lessons.handle_harness_lessons_retrieve(
        context,
        {
            "task_class": "task:retrieval",
            "deficiency": "stale-context",
            "trajectory_features": {"steps": 12.0},
        },
        _op(),
    )
    assert retriever.calls == [
        {
            "context": context,
            "task_class": "task:retrieval",
            "deficiency": "stale-context",
            "trajectory_features": {"steps": 12.0},
        }
    ]
    value = result["value"]
    assert value["availability"] == "available"
    assert value["abstained"] is False
    assert [h["confidence"] for h in value["hits"]] == [0.9, 0.5, 0.1]
    assert [h["what_changed"] for h in value["hits"]] == [
        "first",
        "second",
        "third",
    ]
    assert value["hits"][0]["evidence_run_ids"] == ["run-a", "run-b"]


async def test_empty_engine_answer_abstains(op_by_id) -> None:
    retriever = _LessonRetriever(read=_read())
    result = await harness_lessons.handle_harness_lessons_retrieve(
        harness_context(services={SERVICE: retriever}),
        {"task_class": "t", "deficiency": "d"},
        _op(),
    )
    assert result["value"] == {
        "availability": "available",
        "hits": [],
        "abstained": True,
    }


async def test_unavailable_reader_answer_is_unavailable(op_by_id) -> None:
    retriever = _LessonRetriever(read=UNAVAILABLE_LESSON_RETRIEVAL)
    result = await harness_lessons.handle_harness_lessons_retrieve(
        harness_context(services={SERVICE: retriever}),
        {"task_class": "t", "deficiency": "d"},
        _op(),
    )
    assert result["value"] == UNAVAILABLE_LESSON_RETRIEVAL.model_dump(mode="json")


async def test_malformed_hit_refuses_the_whole_answer(op_by_id) -> None:
    retriever = _LessonRetriever()
    retriever.read = LessonRetrievalRead.model_construct(
        availability="available",
        hits=(_hit(evidence_run_ids=()),),
        abstained=False,
    )
    result = await harness_lessons.handle_harness_lessons_retrieve(
        harness_context(services={SERVICE: retriever}),
        {"task_class": "t", "deficiency": "d"},
        _op(),
    )
    assert result["value"] == UNAVAILABLE_LESSON_RETRIEVAL.model_dump(mode="json")


def test_lesson_hit_refuses_unknown_fields_and_no_evidence() -> None:
    with pytest.raises(ValidationError):
        HarnessLessonHit.model_validate(_hit(unwanted=True))
    with pytest.raises(ValidationError):
        HarnessLessonHit.model_validate(_hit(evidence_run_ids=()))
