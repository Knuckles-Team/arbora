"""No module of the arbora package exceeds the line bound.

ARBORA-HOST-R038.9, ARBORA-HOST-R038.10 and ARBORA-HOST-R038.11 split the multiplexer, the
modules over 2500 lines and the modules between 1500 and 2500 lines until none is
over the bound. This is the bound that stays: the coupling hook does not measure
size, and there is no allowance list, so a module that grows past it is split by
its reason to change, not exempted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "arbora"
LINE_BOUND = 1500


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _over_the_bound() -> dict[str, int]:
    counts = {
        path.relative_to(PACKAGE.parent).as_posix(): _line_count(path)
        for path in sorted(PACKAGE.rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    return {name: count for name, count in counts.items() if count > LINE_BOUND}


@pytest.mark.spec("ARBORA-HOST-R038.9")
@pytest.mark.spec("ARBORA-HOST-R038.10")
@pytest.mark.spec("ARBORA-HOST-R038.11")
def test_no_module_exceeds_the_line_bound() -> None:
    assert _over_the_bound() == {}
