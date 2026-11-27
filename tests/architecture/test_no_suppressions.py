"""ARBORA-HOST-R039.12: a broad catch is narrowed or is a logged boundary.

No source line under ``arbora`` carries a ``noqa: BLE001`` comment, a
``type: ignore`` comment or a ``pragma: no cover`` comment, and ruff's blind-except
rule (``BLE``) is selected and clean: a catch of ``Exception`` either re-raises or
logs its traceback.
"""

from __future__ import annotations

import importlib.util
import io
import shutil
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"
RUFF_PIN = "0.17.5"
SUPPRESSIONS = ("noqa: BLE001", "type: ignore", "pragma: no cover")


def _comment_tokens(source_file: Path) -> list[tokenize.TokenInfo]:
    text = source_file.read_text(encoding="utf-8")
    return [
        token
        for token in tokenize.generate_tokens(io.StringIO(text).readline)
        if token.type == tokenize.COMMENT
    ]


def _markers(source_file: Path) -> list[tuple[int, str]]:
    return [
        (token.start[0], marker)
        for token in _comment_tokens(source_file)
        for marker in SUPPRESSIONS
        if marker in token.string
    ]


@pytest.mark.spec("ARBORA-HOST-R039.12")
def test_no_noqa_ble001_type_ignore_or_pragma_no_cover_remains() -> None:
    offenders = [
        f"{path.relative_to(ARBORA_ROOT.parent)}:{line} {marker}"
        for path in sorted(ARBORA_ROOT.rglob("*.py"))
        for line, marker in _markers(path)
    ]
    assert not offenders, offenders


def _ruff_command() -> list[str]:
    """The pinned ruff: the environment's module, else the pinned release via uvx."""
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    uvx = shutil.which("uvx")
    assert uvx, "ruff is neither installed in this environment nor runnable via uvx"
    return [uvx, f"ruff@{RUFF_PIN}"]


@pytest.mark.spec("ARBORA-HOST-R039.12")
def test_ruff_blind_except_rule_passes_over_arbora() -> None:
    command = _ruff_command()
    result = subprocess.run(
        [*command, "check", "--select", "BLE", "--no-cache", str(ARBORA_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
