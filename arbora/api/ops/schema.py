"""Read-only schema-source views over the engine (ARBORA-OPS-R018.1).

``ontology.schema.list`` returns the graph's schema-source set and
``ontology.schema.graph`` returns one source with its stored documents. Both run
under the ``owl:read`` scope; the engine redacts importer principals and lease ids
from that view and Arbora returns it as sent. ``GraphSchema`` stays the admin
write and is never bound here. An engine without ``GraphSchemaGet`` refuses
``UNAVAILABLE`` through :func:`arbora.api.ops.engine_calls.engine_send`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict, Field

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import OpSpec


class SchemaListParams(Params):
    """No request field: the graph is the verified session's."""


class SchemaGetParams(Params):
    source_id: str = Field(min_length=1, max_length=512)


class SchemaView(Params):
    """The engine's schema view, carried as the engine sent it."""

    model_config = ConfigDict(extra="allow")


def _view(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, Mapping):
        raise unavailable("the engine returned a malformed schema view")
    return dict(answer)


async def handle_schema_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The graph's schema-source set and composed digest (``GraphSchemaList``)."""
    return _view(await engine_send(context, "GraphSchemaList", {}))


async def handle_schema_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One schema source with its stored documents (``GraphSchemaGet``)."""
    request = {"source_id": params["source_id"]}
    return _view(await engine_send(context, "GraphSchemaGet", request))


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="ontology.schema.list",
            summary="List the schema sources the graph carries and its composed digest",
            examples=("list the graph's schema sources",),
            params=SchemaListParams,
            result=SchemaView,
            handler="arbora.api.ops.schema.handle_schema_list",
            scope="owl:read",
        ),
        build_read_op(
            op_id="ontology.schema.graph",
            summary="Read one schema source with its stored shapes and ontology",
            examples=("show this schema source",),
            params=SchemaGetParams,
            result=SchemaView,
            handler="arbora.api.ops.schema.handle_schema_get",
            scope="owl:read",
        ),
    )


specs = operations

__all__ = ["handle_schema_get", "handle_schema_list", "operations", "specs"]
