"""File-part admission over the served A2A route (ARBORA-A2A-R001.5.3, R001.5.4)."""

from __future__ import annotations

import pytest

from arbora.a2a.parts import MediaLimits, sniff_image_type
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
