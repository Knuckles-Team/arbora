"""Caller-bound deterministic and natural-language engine query declarations."""

from __future__ import annotations

from pydantic import Field

from graph_os.api.ops._common import Params
from graph_os.api.registry import EgMethod, EgSchemaRef, OpSpec, Verb


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
            params=EgSchemaRef(
                path="contract/schemas/method.request.json#/methods/Uql/properties/params"
            ),
            result=EgSchemaRef(path="contract/schemas/result.query.json#/methods/Uql"),
            binding=EgMethod(service="Uql", op="Uql"),
            scopes=frozenset({"query:unified"}),
        ),
        OpSpec(
            id="query.nl",
            verb=Verb.ASK,
            summary="Query the caller's graph with the engine natural-language planner",
            examples=("explain why this deployment failed",),
            params=NaturalLanguageParams,
            result=EgSchemaRef(
                path="contract/schemas/result.query.json#/methods/NlQuery"
            ),
            binding=EgMethod(service="NlQuery", op="NlQuery"),
            scopes=frozenset({"query:nl"}),
        ),
    )
