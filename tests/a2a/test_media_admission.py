"""File-part admission over the served A2A route (ARBORA-A2A-R001.5.3, R001.5.4)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.a2a.parts import (
    MediaCapability,
    MediaLimits,
    capability_from_assembly,
    sniff_image_type,
)
from arbora.core.settings import runtime_config
from tests.a2a._parts_support import JPEG, PNG, WEBP, file_part, reason, send, served


def test_signatures_name_the_image_type() -> None:
    assert sniff_image_type(PNG) == "image/png"
    assert sniff_image_type(JPEG) == "image/jpeg"
    assert sniff_image_type(WEBP) == "image/webp"
    assert sniff_image_type(b"GIF89a....") is None


@pytest.mark.spec("ARBORA-A2A-R001.5.3")
def test_mismatched_signature_is_refused_with_zero_tasks() -> None:
    client, authority = served()
    # A JPEG declared as PNG, and an unlisted image type, each create no task.
    assert reason(send(client, [file_part(JPEG, "image/png")])) == (
        "file_part_type_mismatch"
    )
    assert reason(send(client, [file_part(PNG, "image/gif")])) == (
        "file_part_type_unsupported"
    )
    assert reason(send(client, [file_part(b"not an image at all", "image/webp")])) == (
        "file_part_type_mismatch"
    )
    assert authority.dispatched == []


@pytest.mark.spec("ARBORA-A2A-R001.5.4")
def test_request_byte_and_image_count_limits_refuse_with_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, authority = served()
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_image_parts", 2)
    three = [file_part(PNG, "image/png")] * 3
    assert reason(send(client, three)) == "image_part_count"
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_image_parts", 4)
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_request_media_bytes", 1_024)
    big = PNG + b"\x00" * 600
    assert reason(send(client, [file_part(big, "image/png")] * 2)) == (
        "request_media_too_large"
    )
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_file_part_bytes", 1_024)
    assert reason(send(client, [file_part(PNG + b"\x00" * 2_000, "image/png")])) == (
        "file_part_too_large"
    )
    assert authority.dispatched == []


def test_limit_table_is_bounded_to_its_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MediaLimits.from_config().max_image_parts == 4
    monkeypatch.setitem(runtime_config._overrides, "a2a_max_image_parts", 9)
    with pytest.raises(ValueError):
        MediaLimits.from_config()


CAPABLE = MediaCapability(
    revision="a" * 64,
    accepts=(("image/png", 4_096), ("image/jpeg", 4_096)),
    max_parts=4,
)


def _send_revision(client: Any, parts: list[dict[str, Any]], revision: str) -> Any:
    from tests.a2a._parts_support import send_with_metadata

    return send_with_metadata(client, parts, {"arboraModelRevision": revision})


@pytest.mark.spec("ARBORA-A2A-R001.5.2")
def test_capable_model_accepts_png_and_unknown_stale_oversize_create_no_task() -> None:
    client, authority = served(CAPABLE)
    assert send(client, [file_part(PNG, "image/png")]).get("error") is None
    assert len(authority.dispatched) == 1

    # An unlisted type, an oversize part and a stale revision each refuse.
    refused = send(client, [file_part(WEBP, "image/webp")])
    assert refused["error"]["data"]["reason"] == "media_type_not_accepted"
    big = PNG + b"\x00" * 5_000
    assert send(client, [file_part(big, "image/png")])["error"]["data"]["reason"] == (
        "media_part_too_large"
    )
    stale = _send_revision(client, [file_part(PNG, "image/png")], "b" * 64)
    assert stale["error"]["message"] == "POLICY_DENIED"
    assert stale["error"]["data"]["reason"] == "media_capability_stale"
    current = _send_revision(client, [file_part(PNG, "image/png")], "a" * 64)
    assert current.get("error") is None
    assert len(authority.dispatched) == 2

    # An unknown capability never admits an image.
    unknown_client, unknown = served()
    body = send(unknown_client, [file_part(PNG, "image/png")])
    assert body["error"]["message"] == "UNAVAILABLE"
    assert body["error"]["data"]["reason"] == "media_capability_unknown"
    assert unknown.dispatched == []
    # Text parts and parts for the modality seam are never checked against it.
    text_only, text_authority = served()
    assert send(text_only, [file_part(b"hello", "text/plain")]).get("error") is None
    assert len(text_authority.dispatched) == 1


def test_assembly_answer_maps_to_the_capability() -> None:
    from types import SimpleNamespace

    answer = SimpleNamespace(
        model_revision="c" * 64,
        model_input=SimpleNamespace(
            media_types=["image/png", "image/webp"],
            max_aggregate_bytes=900,
            max_parts=2,
        ),
    )
    assert capability_from_assembly(answer) == MediaCapability(
        "c" * 64, (("image/png", 900), ("image/webp", 900)), 2
    )
    # A null model_input, or a client that predates the fields, is unknown.
    assert capability_from_assembly(SimpleNamespace(model_input=None)) is None
    assert capability_from_assembly(SimpleNamespace()) is None
