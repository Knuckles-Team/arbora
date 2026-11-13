"""Caller-bound deterministic and natural-language engine query declarations."""

from __future__ import annotations

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.projections import (
    ProjectionsParams,
    ProjectionsResult,
    handle_query_projections,
)
from arbora.api.ops.query_federation import NaturalLanguageResult
from arbora.api.registry import EgMethod, OpSpec, Verb, eg_method_ref

__all__ = ["NaturalLanguageParams", "handle_query_projections", "operations"]


class NaturalLanguageParams(Params):
    """The engine graph is supplied by the verified session, never the request."""

    text: str = Field(min_length=1)


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
            op_id="query.projections",
            summary="List the schema projections the caller's verified scopes cover",
            examples=("which tables can I query",),
            params=ProjectionsParams,
            result=ProjectionsResult,
            handler="arbora.api.ops.query.handle_query_projections",
            scope="query:read",
        ),
    )
