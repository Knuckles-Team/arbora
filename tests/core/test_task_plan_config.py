"""The task-plan configuration shape lives in core (ARBORA-HOST-R038.6)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_PROBE = """
import sys
from arbora.core.settings import ArboraConfig
config = ArboraConfig(ARBORA_TASK_PLAN={"max_concurrency": 2, "max_steps": 4})
assert config.task_plan == {"max_concurrency": 2, "max_steps": 4}
leaked = sorted(
    m for m in sys.modules
    if m == "arbora.mcp_server" or m.startswith(("arbora.mcp_server.", "arbora.api"))
)
print("LEAKED", leaked)
"""


@pytest.mark.spec("ARBORA-HOST-R038.6")
def test_core_settings_load_without_importing_api_or_mcp_server() -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-800:]
    assert "LEAKED []" in done.stdout


@pytest.mark.spec("ARBORA-HOST-R038.6")
def test_config_shape_validates_and_freezes_without_the_runtime() -> None:
    from arbora.core.task_plan_config import TaskPlanConfig

    config = TaskPlanConfig({"a": [1]}, {"p": {"q": 1}}, 2, 4)
    assert config.feature_schema["a"] == (1,)
    with pytest.raises(TypeError):
        config.policy["p"] = 1  # type: ignore[index]
    for bad in (
        {"max_concurrency": 9, "max_steps": 1},
        {"max_concurrency": 1, "max_steps": 0},
    ):
        with pytest.raises(ValueError, match="between"):
            TaskPlanConfig({}, {}, **bad)


@pytest.mark.spec("ARBORA-HOST-R038.6")
def test_the_runtime_imports_the_core_shape() -> None:
    from arbora.core.task_plan_config import TaskPlanConfig
    from arbora.mcp_server import task_plan

    assert task_plan.TaskPlanConfig is TaskPlanConfig
