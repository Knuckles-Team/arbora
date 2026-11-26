"""Caller-bound deterministic and natural-language engine query declarations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.errors import not_served
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send
from arbora.api.ops.projections import (
    ProjectionsParams,
    ProjectionsResult,
    handle_query_projections,
)
from arbora.api.ops.query_federation import NaturalLanguageResult
from arbora.api.registry import EgMethod, OpSpec, Verb, eg_method_ref

__all__ = [
    "NaturalLanguageParams",
    "handle_cross_source_report",
    "handle_query_projections",
    "operations",
]


class NaturalLanguageParams(Params):
    """The engine graph is supplied by the verified session, never the request."""

    text: str = Field(min_length=1)


class CrossSourceReportParams(Params):
    """The report question; the engine owns every rule of the report."""

    request: dict[str, object] = Field(default_factory=dict)


class CrossSourceReportResult(Params):
    value: dict[str, object]


async def handle_cross_source_report(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward to the engine's cross-source report and return its typed result unchanged."""
    from medulla.generated import METHOD_IDS

    if "CrossSourceReport" not in METHOD_IDS:
        raise not_served("CrossSourceReport", "MEDULLA-CQ-XSRC-R006")
    answer = await engine_send(
        context, "CrossSourceReport", {"request": dict(params["request"])}
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="query.uql",
            verb=Verb.ASK,
            summary="Execute a deterministic UQL query",
            examples=("execute a UQL query",),
            params=eg_method_ref("Uql", "request"),
            result=eg_method_ref("Uql", "result"),
            binding=EgMethod(service="Uql", op="Uql"),
            scopes=frozenset({"query:unified"}),
        ),
        OpSpec(
            id="query.nl",
            verb=Verb.ASK,
            summary="Query the caller's graph with the engine natural-language planner",
            examples=("explain why this deployment failed",),
            params=NaturalLanguageParams,
            # R043.3: the one-door operation explicitly advertises both the
            # stable EG rows-only response and the composed projection.  The
            # served process selects one branch: no planner means native rows;
            # a composed planner means the typed federated view.
            result=NaturalLanguageResult,
            binding=EgMethod(service="NlQuery", op="NlQuery"),
            scopes=frozenset({"query:nl"}),
        ),
        build_read_op(
            op_id="query.cross_source_report",
            summary="Report the supply-chain impact across sources",
            examples=("which sources does this advisory reach",),
            params=CrossSourceReportParams,
            result=CrossSourceReportResult,
            handler="arbora.api.ops.query.handle_cross_source_report",
            scope="query:unified",
        ),
        build_read_op(
            op_id="query.projections",
            summary="List the schema projections the caller's verified scopes cover",
            examples=("which tables can I query",),
            params=ProjectionsParams,
            result=ProjectionsResult,
            handler="arbora.api.ops.query.handle_query_projections",
            scope="query:read",
        ),
    )
