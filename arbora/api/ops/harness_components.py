"""Served harness component reads (ARBORA-OPS-R068.1).

``harness.components.list`` and ``harness.components.get`` serve the
versioned harness components of the engine vocabulary (EG-ORCH-VOCAB-R801)
through the caller-bound engine path under ``kg:read``. The engine is the
sole authority for component versions; Arbora computes no ranking and
stores no harness content. An uncomposed reader fails closed with a
typed refusal; a reader answer that does not project to a valid version
is unavailable, never partial.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops.harness_admission import (
    admission_request_for,
    admit_harness_surface,
    refusal_for,
)
from arbora.api.registry import OpSpec, Verb

__all__ = [
    "ComponentKind",
    "ComponentReader",
    "ComponentVersionEntry",
    "ComponentVersionGetParams",
    "ComponentVersionGetRead",
    "ComponentVersionGetResult",
    "ComponentVersionListParams",
    "ComponentVersionListRead",
    "ComponentVersionListResult",
    "UNAVAILABLE_COMPONENT_GET",
    "UNAVAILABLE_COMPONENT_LIST",
    "handle_harness_components_get",
    "handle_harness_components_list",
    "operations",
    "project_component_entry",
    "specs",
]

#: The closed harness component kinds (EG-ORCH-VOCAB-R801).
ComponentKind = Literal[
    "system_prompt",
    "guardrails",
    "memory",
    "tool_guidance",
    "subagent",
    "skill",
    "script",
    "plugin",
]


class ComponentVersionEntry(BaseModel):
    """One published, immutable harness component version.

    Mirrors the engine vocabulary record; every field is engine-owned.
    A version without evidence, an unknown kind, a confidence outside
    ``[0, 1]`` or an invalid interval refuses the projection.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str = Field(min_length=1, max_length=512)
    kind: ComponentKind
    content_ref: str = Field(min_length=1, max_length=512)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: datetime
    valid_to: datetime | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_run_id: str = Field(min_length=1, max_length=512)
    supersedes_id: str | None = None
    applied_to_id: str | None = None

    @model_validator(mode="after")
    def _valid_interval(self) -> ComponentVersionEntry:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("component version interval must be valid")
        return self


class ComponentVersionListRead(Params):
    """One cursor page of readable component versions, or nothing."""

    availability: Literal["available", "unavailable"]
    entries: tuple[ComponentVersionEntry, ...] = ()
    next_cursor: str | None = None


class ComponentVersionGetRead(Params):
    """One component version at the requested time, or nothing.

    An unknown component and a component of another caller project to the
    same unavailable read; the caller cannot distinguish which.
    """

    availability: Literal["available", "unavailable"]
    entry: ComponentVersionEntry | None = None


UNAVAILABLE_COMPONENT_LIST = ComponentVersionListRead(availability="unavailable")
UNAVAILABLE_COMPONENT_GET = ComponentVersionGetRead(availability="unavailable")


class ComponentVersionListParams(Params):
    """``harness.components.list``: cursor-paged, caller-readable only."""

    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=50, ge=1, le=200)
    kind: ComponentKind | None = None


class ComponentVersionGetParams(Params):
    """``harness.components.get``: one component at an optional instant."""

    component_id: str = Field(min_length=1, max_length=512)
    as_of: datetime | None = None


class ComponentVersionListResult(Params):
    value: dict[str, Any]


class ComponentVersionGetResult(Params):
    value: dict[str, Any]


@runtime_checkable
class ComponentReader(Protocol):
    """The host-composed read port for harness component versions."""

    async def list(
        self,
        *,
        context: Any,
        cursor: str | None,
        limit: int,
        kind: ComponentKind | None,
    ) -> ComponentVersionListRead: ...

    async def get(
        self, *, context: Any, component_id: str, as_of: datetime | None
    ) -> ComponentVersionGetRead: ...


def project_component_entry(raw: object) -> ComponentVersionEntry | None:
    """Validate one reader answer; anything not a valid version is nothing."""
    if isinstance(raw, BaseModel):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, Mapping):
        return None
    try:
        return ComponentVersionEntry.model_validate(raw)
    except ValidationError:
        return None


async def handle_harness_components_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one cursor page of the caller's readable component versions."""
    decision = admit_harness_surface(
        admission_request_for(context.caller, op.id), services=context.services
    )
    if not decision.admitted:
        raise refusal_for(decision)
    request = ComponentVersionListParams.model_validate(dict(params))
    reader: ComponentReader = bound_service(
        context,
        "harness_component_reader",
        reason="harness component reader is not composed",
    )
    read = await reader.list(
        context=context,
        cursor=request.cursor,
        limit=request.limit,
        kind=request.kind,
    )
    if read.availability != "available":
        return {"value": UNAVAILABLE_COMPONENT_LIST.model_dump(mode="json")}
    entries: list[ComponentVersionEntry] = []
    for entry in read.entries:
        projected = project_component_entry(entry)
        if projected is None:
            return {"value": UNAVAILABLE_COMPONENT_LIST.model_dump(mode="json")}
        entries.append(projected)
    read = ComponentVersionListRead(
        availability="available",
        entries=tuple(entries),
        next_cursor=read.next_cursor,
    )
    return {"value": read.model_dump(mode="json")}


async def handle_harness_components_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one component version, or an unavailable read with no entry."""
    decision = admit_harness_surface(
        admission_request_for(context.caller, op.id), services=context.services
    )
    if not decision.admitted:
        raise refusal_for(decision)
    request = ComponentVersionGetParams.model_validate(dict(params))
    reader: ComponentReader = bound_service(
        context,
        "harness_component_reader",
        reason="harness component reader is not composed",
    )
    read = await reader.get(
        context=context,
        component_id=request.component_id,
        as_of=request.as_of,
    )
    entry = project_component_entry(read.entry)
    if read.availability != "available" or entry is None:
        return {"value": UNAVAILABLE_COMPONENT_GET.model_dump(mode="json")}
    read = ComponentVersionGetRead(availability="available", entry=entry)
    return {"value": read.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            verb=Verb.FIND,
            op_id="harness.components.list",
            summary="List the caller's readable harness component versions",
            examples=("what harness component versions can I read",),
            params=ComponentVersionListParams,
            result=ComponentVersionListResult,
            handler="arbora.api.ops.harness_components.handle_harness_components_list",
            scope="kg:read",
        ),
        build_read_op(
            verb=Verb.FIND,
            op_id="harness.components.get",
            summary="Read one harness component version at an instant",
            examples=("read the current guardrails component version",),
            params=ComponentVersionGetParams,
            result=ComponentVersionGetResult,
            handler="arbora.api.ops.harness_components.handle_harness_components_get",
            scope="kg:read",
        ),
    )


specs = operations
