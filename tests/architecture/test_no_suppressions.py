"""ARBORA-HOST-R039.12: a broad catch is narrowed or is a logged boundary.

No source line under ``arbora`` carries a ``noqa: BLE001`` comment. A catch that
cannot name the exceptions its call raises raises a typed refusal or logs its
cause; it does not suppress the lint rule.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"


def _suppressions(source_file: Path) -> list[int]:
    text = source_file.read_text(encoding="utf-8")
    found: list[int] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and "noqa: BLE001" in token.string:
            found.append(token.start[0])
    return found


@pytest.mark.spec("ARBORA-HOST-R039.12")
def test_no_noqa_ble001_remains() -> None:
    offenders = [
        f"{path.relative_to(ARBORA_ROOT.parent)}:{line}"
        for path in sorted(ARBORA_ROOT.rglob("*.py"))
        for line in _suppressions(path)
    ]
    assert not offenders, offenders
