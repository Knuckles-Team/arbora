"""Native harness substrate reads under the admitted caller's authority.

Host adapters for the served harness component and improvement library
surface (ARBORA-OPS-R068). The engine is the sole authority for
versions, contrasts and lessons: these adapters hold no harness content,
compute no ranking, and retain no caller, client or session. A pinned
engine distribution that lacks the generated wire methods reads as
``unavailable``; nothing falls back to local state or an advisory
answer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from arbora.api.invoke.bootstrap import _caller_dispatch_graph
from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import claim_values
from arbora.api.ops.harness_components import (
    UNAVAILABLE_COMPONENT_GET,
    UNAVAILABLE_COMPONENT_LIST,
    ComponentVersionEntry,
    ComponentVersionGetRead,
    ComponentVersionListRead,
)
from arbora.api.ops.harness_evolution import (
    UNAVAILABLE_EVOLUTION_REPORT,
    EvolutionReport,
    EvolutionReportRead,
)
from arbora.api.ops.harness_lessons import (
    UNAVAILABLE_LESSON_RETRIEVAL,
    HarnessLessonHit,
    LessonRetrievalRead,
)

__all__ = [
    "NativeComponentReader",
    "NativeEvolutionReportReader",
    "NativeLessonRetriever",
]

#: The generated wire module of the installed engine distribution. The
#: engine vocabulary and decision rows own the final wire names
#: (EG-ORCH-VOCAB-R801, EG-ORCH-VOCAB-R802.3, EG-DECISION-ENGINE-R803);
#: a distribution lacking this module or any of the senders reads as
#: unavailable.
_ORCH_MODULE = "epistemic_graph.generated.orchestration"


async def _native_read(
    client: Any, *, sender: str, decoder: str, payload: dict[str, Any], graph: str
) -> Any:
    """One caller-bound engine read; an older distribution answers None."""
    # Older published engine distributions cannot provide these queries yet.
    try:
        from importlib import import_module

        module = import_module(_ORCH_MODULE)
        send = getattr(module, sender)
        decode = getattr(module, decoder)
    except (ImportError, AttributeError):
        return None
    from epistemic_graph import EngineResponseError
    from epistemic_graph.generated import ContractViolation

    from arbora.api.errors import EngineRefusal

    try:
        result = await send(client, payload, graph)
    except EngineResponseError as exc:
        raise EngineRefusal(exc.code) from exc
    try:
        return decode(result)
    except (ContractViolation, ValueError):
        return None


def _validate[M: BaseModel](model: type[M], raw: Any) -> M | None:
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, dict):
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


class NativeComponentReader:
    """One stateless component reader shared by the serving root."""

    async def list(
        self,
        *,
        context: ExecutionContext,
        cursor: str | None,
        limit: int,
        kind: str | None,
    ) -> ComponentVersionListRead:
        graph = await _caller_dispatch_graph(context)
        if "kg:read" not in context.caller.effective_scopes:
            raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": ["kg:read"]})
        with context.client.use_verified_context(
            claim_values(context.caller.engine_claims)
        ):
            raw = await _native_read(
                context.client,
                sender="send_list_harness_components",
                decoder="decode_list_harness_components",
                payload={"cursor": cursor, "limit": limit, "kind": kind},
                graph=graph,
            )
        if raw is None:
            return UNAVAILABLE_COMPONENT_LIST
        entries_raw = raw if isinstance(raw, list) else None
        if entries_raw is None:
            return UNAVAILABLE_COMPONENT_LIST
        entries: list[ComponentVersionEntry] = []
        for entry in entries_raw:
            projected = _validate(ComponentVersionEntry, entry)
            if projected is None:
                return UNAVAILABLE_COMPONENT_LIST
            entries.append(projected)
        return ComponentVersionListRead(
            availability="available", entries=tuple(entries)
        )

    async def get(
        self,
        *,
        context: ExecutionContext,
        component_id: str,
        as_of: datetime | None,
    ) -> ComponentVersionGetRead:
        graph = await _caller_dispatch_graph(context)
        if "kg:read" not in context.caller.effective_scopes:
            raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": ["kg:read"]})
        with context.client.use_verified_context(
            claim_values(context.caller.engine_claims)
        ):
            raw = await _native_read(
                context.client,
                sender="send_current_harness_component",
                decoder="decode_current_harness_component",
                payload={
                    "component_id": component_id,
                    "as_of": as_of.isoformat() if as_of is not None else None,
                },
                graph=graph,
            )
        entry = _validate(ComponentVersionEntry, raw)
        if entry is None:
            return UNAVAILABLE_COMPONENT_GET
        return ComponentVersionGetRead(availability="available", entry=entry)


class NativeLessonRetriever:
    """One stateless improvement-library retriever for the serving root."""

    async def retrieve(
        self,
        *,
        context: ExecutionContext,
        task_class: str,
        deficiency: str,
        trajectory_features: dict[str, float],
    ) -> LessonRetrievalRead:
        graph = await _caller_dispatch_graph(context)
        if "kg:read" not in context.caller.effective_scopes:
            raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": ["kg:read"]})
        with context.client.use_verified_context(
            claim_values(context.caller.engine_claims)
        ):
            raw = await _native_read(
                context.client,
                sender="send_retrieve_lessons",
                decoder="decode_retrieve_lessons",
                payload={
                    "task_class": task_class,
                    "deficiency": deficiency,
                    "trajectory_features": trajectory_features,
                },
                graph=graph,
            )
        if raw is None:
            return UNAVAILABLE_LESSON_RETRIEVAL
        if not isinstance(raw, (list, tuple)):
            return UNAVAILABLE_LESSON_RETRIEVAL
        hits: list[HarnessLessonHit] = []
        for hit in raw:
            projected = _validate(HarnessLessonHit, hit)
            if projected is None:
                return UNAVAILABLE_LESSON_RETRIEVAL
            hits.append(projected)
        return LessonRetrievalRead(
            availability="available", hits=tuple(hits), abstained=not hits
        )


class NativeEvolutionReportReader:
    """One stateless evolution-report reader for the serving root."""

    async def report(
        self, *, context: ExecutionContext, task_ref: str
    ) -> EvolutionReportRead:
        graph = await _caller_dispatch_graph(context)
        if "kg:read" not in context.caller.effective_scopes:
            raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": ["kg:read"]})
        with context.client.use_verified_context(
            claim_values(context.caller.engine_claims)
        ):
            raw = await _native_read(
                context.client,
                sender="send_harness_evolution_report",
                decoder="decode_harness_evolution_report",
                payload={"task_ref": task_ref},
                graph=graph,
            )
        report = _validate(EvolutionReport, raw)
        if report is None:
            return UNAVAILABLE_EVOLUTION_REPORT
        return EvolutionReportRead(availability="available", report=report)
