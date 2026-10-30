"""The one mypy hook type-checks the test suite (ARBORA-DEPLOY-R004)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".pre-commit-config.yaml"
LOCAL_HOOK = ROOT / "scripts" / "local_hook.sh"


@pytest.mark.spec("ARBORA-DEPLOY-R004")
def test_mypy_hook_does_not_exclude_the_test_suite() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"] if "mypy" in hook["id"]]
    # One type check: the locked-environment hook. A second, isolated mypy
    # environment duplicated it and cost about two and a half minutes a run.
    assert [hook["id"] for hook in hooks] == ["mypy-env"]
    assert "tests" not in str(hooks[0].get("exclude", ""))
    assert "uv run --no-sync mypy arbora tests" in LOCAL_HOOK.read_text(encoding="utf-8")
    assert "tests" not in str(config.get("exclude", ""))
