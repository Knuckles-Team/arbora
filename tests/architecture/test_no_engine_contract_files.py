"""Arbora reads no engine contract file; operations bind to the run-time catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.spec("ARBORA-FLEET-R012.1")
def test_no_source_reads_the_packaged_engine_contract() -> None:
    markers = ("contract/schemas", "contract/methods", '"contract")', "scopes.json")
    readers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "arbora").rglob("*.py")
        if any(marker in path.read_text(encoding="utf-8") for marker in markers)
    )
    assert readers == []
