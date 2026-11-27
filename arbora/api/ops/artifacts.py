"""Artifact operations for engine authentication mode (ARBORA-OPS-R881).

``artifact.create``, ``artifact.get`` and ``artifact.refresh`` call one ``artifact_store``
port declared here; arbora imports no meristem store module. A caller without a store
refuses ``UNAVAILABLE``; an unknown identifier refuses ``NOT_FOUND`` like a foreign one.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op, build_write_op
from arbora.api.registry import OpSpec, Verb


class ArtifactCreateParams(Params):
    template: str = Field(min_length=1, max_length=256)
    source: dict[str, Any] = Field(default_factory=dict)


class ArtifactRefParams(Params):
    artifact_id: str = Field(min_length=1, max_length=256)


class ArtifactResult(Params):
    value: dict[str, Any]


@runtime_checkable
class ArtifactStore(Protocol):
    async def create(
        self, *, tenant: str, template: str, source: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def get(self, *, tenant: str, artifact_id: str) -> dict[str, Any] | None: ...

    async def refresh(
        self, *, tenant: str, artifact_id: str
    ) -> dict[str, Any] | None: ...


def _store(context: Any) -> ArtifactStore:
    store: ArtifactStore = bound_service(
        context, "artifact_store", reason="artifact store is not configured"
    )
    return store


def _found(artifact: dict[str, Any] | None) -> dict[str, Any]:
    if artifact is None:
        raise OperationRefused("NOT_FOUND")
    return {"value": artifact}


async def handle_artifact_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    created = await _store(context).create(
        tenant=context.caller.tenant,
        template=params["template"],
        source=dict(params["source"]),
    )
    return {"value": created}


async def handle_artifact_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return _found(
        await _store(context).get(
            tenant=context.caller.tenant, artifact_id=params["artifact_id"]
        )
    )


async def handle_artifact_refresh(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return _found(
        await _store(context).refresh(
            tenant=context.caller.tenant, artifact_id=params["artifact_id"]
        )
    )


#: The artifact store owns each record's replay under the caller's key.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "artifact.create": "arbora.api.ops.artifacts:ArtifactStore",
        "artifact.refresh": "arbora.api.ops.artifacts:ArtifactStore",
    }
)


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="artifact.create",
            verb=Verb.ACT,
            summary="Create one live artifact",
            examples=("create an artifact",),
            params=ArtifactCreateParams,
            result=ArtifactResult,
            handler="arbora.api.ops.artifacts.handle_artifact_create",
            scope="blob:write",
        ),
        build_read_op(
            op_id="artifact.get",
            summary="Read one live artifact",
            examples=("show this artifact",),
            params=ArtifactRefParams,
            result=ArtifactResult,
            handler="arbora.api.ops.artifacts.handle_artifact_get",
            scope="blob:read",
        ),
        build_write_op(
            op_id="artifact.refresh",
            verb=Verb.ACT,
            summary="Refresh one live artifact from its source",
            examples=("refresh this artifact",),
            params=ArtifactRefParams,
            result=ArtifactResult,
            handler="arbora.api.ops.artifacts.handle_artifact_refresh",
            scope="blob:write",
        ),
    )


specs = operations

__all__ = ["NATIVE_IDEMPOTENCY", "ArtifactStore", "operations", "specs"]
