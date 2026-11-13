"""Authorization-segmented schema projections (ARBORA-OPS-R052).

The engine owns the boundaries: each projection names its boundary, the
scopes a caller must hold, the tables it covers and the mapping that exposes
them (``Method::SchemaProjections``, list variant). Arbora infers no boundary
and no scope; it only checks the engine's answer against the caller's
verified scopes and returns the covered projections.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import FrozenState, Params, bound_service
from arbora.api.registry import OpSpec

_METHOD = "SchemaProjections"


class SchemaProjection(FrozenState):
    """One authorization boundary of the caller's schema, as the engine lists it."""

    boundary: str = Field(min_length=1, max_length=256)
    required_scopes: tuple[str, ...] = Field(min_length=1, max_length=16)
    tables: tuple[str, ...] = Field(min_length=1, max_length=1024)
    mapping_ref: str = Field(min_length=1, max_length=256)


class ProjectionsParams(Params):
    """``query.projections`` takes no request fields: the caller is the scope."""


class ProjectionsResult(Params):
    projections: tuple[SchemaProjection, ...]


class ProjectionReader(Protocol):
    """The engine read of the caller's schema boundaries."""

    async def list(self, context: Any) -> tuple[SchemaProjection, ...]: ...


def scope_covers(grants: Iterable[str], projection: SchemaProjection) -> bool:
    """True when every scope the projection requires is among ``grants``."""
    held = frozenset(grants)
    return all(scope in held for scope in projection.required_scopes)


def _entry(value: Any) -> SchemaProjection:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return SchemaProjection(
        boundary=value["boundary_id"],
        required_scopes=tuple(value["required_scopes"]),
        tables=tuple(value["tables"]),
        mapping_ref=value["mapping_ref"],
    )


class EngineProjectionReader:
    """Send the engine list variant under the verified caller's own session."""

    async def list(self, context: Any) -> tuple[SchemaProjection, ...]:
        from medulla import EngineResponseError

        from arbora.api.errors import EngineRefusal
        from arbora.api.invoke.bootstrap import dispatch_eg_method
        from arbora.api.registry import EgMethod

        try:
            result = await dispatch_eg_method(
                EgMethod(service=_METHOD, op=_METHOD), {"op": {"op": "list"}}, context
            )
        except EngineResponseError as exc:
            raise EngineRefusal(exc.code) from exc
        value = getattr(result, "payload", result)
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return tuple(_entry(entry) for entry in value.get("entries", ()))


async def handle_query_projections(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return the projections the caller's verified scopes cover.

    No boundary from the engine is ``UNAVAILABLE``; boundaries none of which
    the caller covers is ``SUBJECT_ACCESS_DENIED``. Neither refusal carries a
    count or a name of an uncovered projection.
    """
    reader: ProjectionReader = bound_service(
        context, "projection_reader", reason="schema projection reader is not composed"
    )
    listed = await reader.list(context)
    if not listed:
        raise OperationRefused("UNAVAILABLE", {"reason": "no schema boundaries"})
    grants = context.caller.effective_scopes
    covered = tuple(item for item in listed if scope_covers(grants, item))
    if not covered:
        raise OperationRefused("SUBJECT_ACCESS_DENIED")
    return ProjectionsResult(projections=covered).model_dump(mode="json")
