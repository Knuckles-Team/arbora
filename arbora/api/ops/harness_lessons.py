"""Served improvement-library lesson query (ARBORA-OPS-R068.2).

``harness.lessons.retrieve`` calls the improvement-library query of the
engine vocabulary (EG-ORCH-VOCAB-R802.3) with the task class, the
deficiency and the trajectory features. The engine ranks the hits by
confidence; Arbora preserves that order and computes no ranking. An
empty engine answer abstains. An uncomposed retriever fails closed with
a typed refusal; a reader answer that does not project to valid hits is
unavailable, never partial.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops.harness_admission import (
    admission_request_for,
    admit_harness_surface,
    refusal_for,
)
from arbora.api.ops.harness_components import ComponentKind
from arbora.api.registry import OpSpec, Verb

__all__ = [
    "HarnessLessonHit",
    "HarnessLessonsRetrieveParams",
    "LessonRetrievalRead",
    "LessonRetrievalResult",
    "LessonRetriever",
    "UNAVAILABLE_LESSON_RETRIEVAL",
    "handle_harness_lessons_retrieve",
    "operations",
    "project_lesson_hit",
    "specs",
]


class HarnessLessonHit(BaseModel):
    """One retained improvement lesson, engine-ranked.

    Mirrors the engine vocabulary lesson record; a lesson without
    evidence runs or with unknown fields refuses the projection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1, max_length=256)
    deficiency: str = Field(min_length=1, max_length=512)
    situation: str = Field(min_length=1, max_length=4096)
    changed_component_kind: ComponentKind
    what_changed: str = Field(min_length=1, max_length=4096)
    applicability: str = Field(min_length=1, max_length=4096)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_run_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    supersedes_id: str | None = None


class LessonRetrievalRead(Params):
    """Engine-ranked hits for one lesson query, or nothing.

    ``abstained`` is true when the engine answer carried no hits: an
    empty answer abstains, it never fabricates a hit.
    """

    availability: Literal["available", "unavailable"]
    hits: tuple[HarnessLessonHit, ...] = ()
    abstained: bool = False


UNAVAILABLE_LESSON_RETRIEVAL = LessonRetrievalRead(availability="unavailable")


class HarnessLessonsRetrieveParams(Params):
    """``harness.lessons.retrieve``: one improvement-library query."""

    task_class: str = Field(min_length=1, max_length=256)
    deficiency: str = Field(min_length=1, max_length=512)
    trajectory_features: dict[str, float] = Field(default_factory=dict, max_length=64)


class LessonRetrievalResult(Params):
    value: dict[str, Any]


@runtime_checkable
class LessonRetriever(Protocol):
    """The host-composed read port for the improvement-library query."""

    async def retrieve(
        self,
        *,
        context: Any,
        task_class: str,
        deficiency: str,
        trajectory_features: dict[str, float],
    ) -> LessonRetrievalRead: ...


def project_lesson_hit(raw: object) -> HarnessLessonHit | None:
    """Validate one reader answer; anything not a valid lesson is nothing."""
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, Mapping):
        return None
    try:
        return HarnessLessonHit.model_validate(raw)
    except ValidationError:
        return None


async def handle_harness_lessons_retrieve(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return engine-ranked lesson hits, or abstain, or nothing at all."""
    decision = admit_harness_surface(
        admission_request_for(context.caller, op.id), services=context.services
    )
    if not decision.admitted:
        raise refusal_for(decision)
    request = HarnessLessonsRetrieveParams.model_validate(dict(params))
    retriever: LessonRetriever = bound_service(
        context,
        "harness_lesson_retriever",
        reason="harness lesson retriever is not composed",
    )
    read = await retriever.retrieve(
        context=context,
        task_class=request.task_class,
        deficiency=request.deficiency,
        trajectory_features=request.trajectory_features,
    )
    if read.availability != "available":
        return {"value": UNAVAILABLE_LESSON_RETRIEVAL.model_dump(mode="json")}
    hits: list[HarnessLessonHit] = []
    for hit in read.hits:
        projected = project_lesson_hit(hit)
        if projected is None:
            return {"value": UNAVAILABLE_LESSON_RETRIEVAL.model_dump(mode="json")}
        hits.append(projected)
    read = LessonRetrievalRead(
        availability="available",
        hits=tuple(hits),
        abstained=not hits,
    )
    return {"value": read.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.ASK,
            op_id="harness.lessons.retrieve",
            summary="Retrieve ranked improvement lessons for one deficiency",
            examples=("what lessons improve this kind of task",),
            params=HarnessLessonsRetrieveParams,
            result=LessonRetrievalResult,
            handler="arbora.api.ops.harness_lessons.handle_harness_lessons_retrieve",
            scope="kg:read",
        ),
    )


specs = operations
