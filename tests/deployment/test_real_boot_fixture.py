"""Exercise the installed-wheel fixture bundle without booting an engine."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
import yaml

from tests.test_real_boot import _free_port, _terminate, _wait_for_model

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.spec("ARBORA-OPS-R001")


def test_wheel_boot_bundle_starts_the_model_and_serves_completions(tmp_path: Path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["install-smoke"]["steps"]
        if step.get("name") == "Real boot of the installed wheel (linux x86_64)"
    )
    copy = next(
        line.strip() for line in step["run"].splitlines() if "cp tests/" in line
    )
    subprocess.run(
        ["bash", "-c", copy],
        cwd=ROOT,
        env={**os.environ, "boot": str(tmp_path)},
        check=True,
        timeout=10,
    )
    source = ast.parse((tmp_path / "test_real_boot.py").read_text())
    helpers = {
        node.args[0].value
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "with_name"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    for helper in helpers:
        assert (tmp_path / helper).is_file(), f"boot bundle is missing {helper}"

    port = _free_port()
    log_path = tmp_path / "model.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(tmp_path / "_boot_model.py"), str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_model(process, port, log_path)
            for stream in (False, True):
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    data=json.dumps({"model": "boot-model", "stream": stream}).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    body = response.read().decode()
                assert "https://example.org/arbora-boot" in body
                if stream:
                    assert "data: [DONE]" in body
                else:
                    assert json.loads(body)["choices"][0]["finish_reason"] == "stop"
        finally:
            _terminate(process)


def test_missing_model_helper_reports_startup_failure(tmp_path: Path):
    log_path = tmp_path / "model.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(tmp_path / "missing_model.py"), "0"],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            process.wait(timeout=10)
            with pytest.raises(
                AssertionError, match="model fixture exited during startup"
            ) as exc:
                _wait_for_model(process, _free_port(), log_path)
            assert "missing_model.py" in str(exc.value)
        finally:
            _terminate(process)
