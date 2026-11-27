"""The installed launcher validates configuration before it opens anything (ARBORA-HOST-R010.3, R010.6)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_WRAPPER = """
import atexit, json, socket, subprocess, sys
counts = {"listen": 0, "children": 0}
_listen = socket.socket.listen
def listen(self, *a, **k):
    counts["listen"] += 1
    return _listen(self, *a, **k)
socket.socket.listen = listen
_popen = subprocess.Popen.__init__
def popen(self, *a, **k):
    counts["children"] += 1
    return _popen(self, *a, **k)
subprocess.Popen.__init__ = popen
atexit.register(lambda: print("COUNTS " + json.dumps(counts), file=sys.stderr))
mode = sys.argv[1]
sys.argv = ["arbora", "--transport", "streamable-http", "--port", "0"]
if mode == "launch":
    from arbora.mcp_server.server import mcp_server
    mcp_server()
else:
    from arbora.core.environment_manifest import enforce_startup
    print("DIGEST " + enforce_startup().digest)
"""
SENTINEL = "sentinel-value-7731"


def _run(
    tmp_path: Path, mode: str, extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "launch.py"
    script.write_text(_WRAPPER, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        "ARBORA_CONFIG_DIR": str(tmp_path / "config"),
        "ARBORA_DATA_DIR": str(tmp_path / "data"),
        **extra,
    }
    return subprocess.run(  # noqa: S603
        [sys.executable, str(script), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _counts(stderr: str) -> dict[str, int]:
    line = next(x for x in stderr.splitlines() if x.startswith("COUNTS "))
    return json.loads(line.removeprefix("COUNTS "))


@pytest.mark.spec("ARBORA-HOST-R010.3")
@pytest.mark.parametrize(
    ("env", "code", "name"),
    [
        (
            {"DEPLOYMENT_PROFILE": SENTINEL},
            "CONFIG_PROFILE_UNKNOWN",
            "DEPLOYMENT_PROFILE",
        ),
        ({"MAX_UPLOAD_SIZE": SENTINEL}, "CONFIG_MALFORMED", "MAX_UPLOAD_SIZE"),
    ],
)
def test_refusals_start_nothing(
    tmp_path: Path, env: dict[str, str], code: str, name: str
) -> None:
    done = _run(tmp_path, "launch", env)
    assert done.returncode == 2
    assert f"{code}: arbora:{name}" in done.stderr
    assert SENTINEL not in done.stdout + done.stderr
    assert _counts(done.stderr) == {"listen": 0, "children": 0}


@pytest.mark.spec("ARBORA-HOST-R010.6")
def test_valid_and_invalid_launch_after_restart(tmp_path: Path) -> None:
    refused = _run(tmp_path, "launch", {"DEPLOYMENT_PROFILE": SENTINEL})
    assert refused.returncode == 2
    assert SENTINEL not in refused.stderr
    first = _run(tmp_path, "gate", {"DEPLOYMENT_PROFILE": "tiny"})
    again = _run(tmp_path, "gate", {"DEPLOYMENT_PROFILE": "tiny"})
    assert first.returncode == again.returncode == 0
    digest = [x for x in first.stdout.splitlines() if x.startswith("DIGEST ")]
    assert digest == [x for x in again.stdout.splitlines() if x.startswith("DIGEST ")]
    assert len(digest[0]) == len("DIGEST ") + 64
    assert _counts(first.stderr) == {"listen": 0, "children": 0}
    after = _run(tmp_path, "launch", {"DEPLOYMENT_PROFILE": SENTINEL})
    assert after.returncode == 2
    assert _counts(after.stderr) == {"listen": 0, "children": 0}
