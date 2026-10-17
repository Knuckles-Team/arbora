"""Fail-closed strict base model shared by the scaling and placement contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ProtocolModel(BaseModel):
    """Immutable, strict, extra-forbidding base for scaling contract DTOs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
