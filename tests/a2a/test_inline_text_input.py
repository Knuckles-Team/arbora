"""Bounded inline text input over the served A2A route (ARBORA-A2A-R001.5.1)."""

from __future__ import annotations

import pytest

from tests.a2a._parts_support import file_part, reason, send, served


@pytest.mark.spec("ARBORA-A2A-R001.5.1")
def test_inline_text_files_are_bounded_and_oversize_creates_no_task() -> None:
    client, authority = served()
    ok = send(client, [file_part(b"hello", "text/plain")] * 8)
    assert "result" in ok and len(authority.dispatched) == 1
    note = send(client, [file_part(b"# title", "text/markdown")])
    assert "result" in note and len(authority.dispatched) == 2

    nine = [file_part(b"x", "text/plain")] * 9
    assert reason(send(client, nine)) == "file_part_count"
    half = b"a" * 40_000
    assert reason(send(client, [file_part(half, "text/plain")] * 2)) == (
        "file_part_too_large"
    )
    assert reason(send(client, [file_part(b"x", "text/html")])) == (
        "file_part_type_unsupported"
    )
    assert len(authority.dispatched) == 2
