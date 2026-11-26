"""Arbora adopts the shared complexity gate with its fixed caps (ARBORA-DEPLOY-R010.4)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE_KEYS = {"census", "census_paths"}


@pytest.mark.spec("ARBORA-DEPLOY-R010.4")
def test_arbora_config_declares_both_rules() -> None:
    table = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["complexity"]
    assert set(table) <= GATE_KEYS, (
        "the gate's caps are fixed; no cap key may be declared"
    )
    hooks = (ROOT / ".pre-commit-config.yaml").read_text()
    assert "- id: complexity" in hooks
