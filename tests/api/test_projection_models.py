"""Projection model bounds and immutability (ARBORA-OPS-R052.1)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.ops.projections import SchemaProjection

pytestmark = pytest.mark.spec("ARBORA-OPS-R052.1")

VALID: dict[str, Any] = {
    "boundary": "sales",
    "required_scopes": ("data:sales",),
    "tables": ("orders",),
    "mapping_ref": "mapping:sales",
}


def test_fields_are_validated_and_frozen() -> None:
    projection = SchemaProjection.model_validate(VALID)
    with pytest.raises(ValidationError):
        projection.boundary = "other"
    for field, bad in (
        ("boundary", ""),
        ("required_scopes", ()),
        ("required_scopes", tuple(f"s:{i}" for i in range(17))),
        ("tables", ()),
        ("mapping_ref", ""),
    ):
        with pytest.raises(ValidationError):
            SchemaProjection.model_validate({**VALID, field: bad})
    with pytest.raises(ValidationError):
        SchemaProjection.model_validate({**VALID, "extra": "x"})
