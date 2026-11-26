"""Arbora configures the shared complexity gate's two rules (ARBORA-DEPLOY-R010.4)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.spec("ARBORA-DEPLOY-R010.4")
def test_arbora_config_declares_both_rules() -> None:
    table = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["complexity"]
    assert table["max_function_arguments"] == 7
    assert table["max_try_block_statements"] == 5
