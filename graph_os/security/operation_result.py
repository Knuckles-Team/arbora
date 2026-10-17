"""The strict success, failure and redirect envelope of served operations.

Every public failure that :mod:`graph_os.security.error_surface` builds has this
shape, and request DTOs that must refuse unknown fields extend
:class:`ProtocolModel`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["OperationError", "OperationResult", "ProtocolModel"]


class ProtocolModel(BaseModel):
    """Fail-closed base for strict served DTOs: frozen, strict, no extra fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OperationError(ProtocolModel):
    """The stable, privacy-safe error inside an :class:`OperationResult`."""

    code: Annotated[str, Field(min_length=1)]
    retryable: bool
    correlation_id: Annotated[str, Field(min_length=1)]
    detail_ref: Annotated[str, Field(min_length=1)] | None


class OperationResult(ProtocolModel):
    """The success, failure or redirect envelope of one served operation."""

    schema_version: Literal["1"]
    operation_id: Annotated[str, Field(min_length=1)]
    status: Literal["succeeded", "failed", "redirected"]
    result_kind: Annotated[str, Field(min_length=1)] | None
    result_ref: Annotated[str, Field(min_length=1)] | None
    error: OperationError | None
    redirect: dict[str, Any] | None
