"""Upload an image once and send it by reference (ARBORA-A2A-R001.5.5).

``a2a.media.upload`` stores the bytes by digest through the bound media store and
returns ``{digest, media_type, bytes, expires_at}``. The reference is bound to the
verified caller and tenant; Arbora holds no store and fetches no network address.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import Field, field_validator

from arbora.api.domain.a2a.media_refs import MediaStoreUnavailable
from arbora.api.domain.a2a.parts import IMAGE_TYPES, MediaLimits, sniff_image_type
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.registry import OpSpec


class MediaUploadParams(_common.Params):
    media_type: str = Field(min_length=1, max_length=64)
    data: str = Field(min_length=4, max_length=45_000_000)

    @field_validator("data")
    @classmethod
    def _base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("data must be base64") from None
        return value


class MediaUploadReceipt(_common.Params):
    digest: str
    media_type: str
    bytes: int
    expires_at: str


class MediaUploadResult(_common.Params):
    value: MediaUploadReceipt


async def handle_media_upload(
    context: Any,
    params: Mapping[str, Any],
    op: OpSpec,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Store one image under the caller's tenant and return its reference."""
    request = MediaUploadParams.model_validate(dict(params))
    store = _common.bound_service(
        context, "media_ref_store", reason="media store unbound"
    )
    data = base64.b64decode(request.data, validate=True)
    limits = MediaLimits.from_config()
    if request.media_type not in IMAGE_TYPES or (
        sniff_image_type(data) != request.media_type
    ):
        raise OperationRefused(
            "INVALID_ARGUMENT", {"reason": "file_part_type_mismatch"}
        )
    if len(data) > limits.max_file_part_bytes:
        raise OperationRefused("INVALID_ARGUMENT", {"reason": "file_part_too_large"})
    owner = f"{context.caller.tenant}:{context.caller.principal}"
    try:
        receipt = await store.put(owner, request.media_type, data, clock())
    except MediaStoreUnavailable:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "media store unavailable"}
        ) from None
    return {"value": receipt.as_dict()}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_write_op(
            op_id="a2a.media.upload",
            summary="Upload an image and get a reference to send with an A2A message",
            examples=("upload an image for a message",),
            params=MediaUploadParams,
            result=MediaUploadResult,
            handler="arbora.api.ops.a2a_media.handle_media_upload",
            scope="ingest:write",
        ),
    )


specs = operations

__all__ = [
    "MediaUploadParams",
    "MediaUploadResult",
    "handle_media_upload",
    "operations",
    "specs",
]
