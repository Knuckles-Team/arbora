"""The strict success, failure and redirect envelope of served operations.

Every public failure that :mod:`arbora.security.error_surface` builds has this
shape, and request DTOs that must refuse unknown fields extend
:class:`ProtocolModel`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["OpError", "OpResult", "OperationError", "OperationResult", "ProtocolModel"]


class ProtocolModel(BaseModel):
    """Fail-closed base for strict served DTOs: frozen, strict, no extra fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OperationError(ProtocolModel):
    """The stable, privacy-safe error inside an :class:`OperationResult`."""

    code: Annotated[str, Field(min_length=1)]
    retryable: bool
    correlation_id: Annotated[str, Field(min_length=1)]
    detail_ref: Annotated[str, Field(min_length=1)] | None


class OperationStatus(StrEnum):
    """How a served operation ended."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REDIRECTED = "redirected"


class OperationResult(ProtocolModel):
    """The success, failure or redirect envelope of one served operation."""

    schema_version: Literal["1"]
    operation_id: Annotated[str, Field(min_length=1)]
    status: Annotated[OperationStatus, Field(strict=False)]
    result_kind: Annotated[str, Field(min_length=1)] | None
    result_ref: Annotated[str, Field(min_length=1)] | None
    error: OperationError | None
    redirect: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class OpError:
    code: str
    details: Mapping[str, Any] = field(default_factory=dict)
    source: Literal["arbora", "engine", "fleet"] = "arbora"


@dataclass(frozen=True, slots=True)
class OpResult:
    value: Any = None
    code: str = "OK"
    details: Mapping[str, Any] = field(default_factory=dict)
