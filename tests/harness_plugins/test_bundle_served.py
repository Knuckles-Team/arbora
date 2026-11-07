"""Hot-path proofs through the console script (R001.7, R002.5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from arbora.harness_plugins.profiles import profile_ids
from arbora.harness_plugins.validate import validate_tree
from tests.harness_plugins.conftest import ENDPOINT, write_skill


def arbora(
    *argv: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from arbora.console import main; sys.argv[0] = 'arbora'; main()",
            *argv,
        ],
        capture_output=True,
        text=True,
        env=env or dict(os.environ),
        timeout=120,
        check=False,
    )


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R001.7")
def test_console_builds_every_profile_and_each_validates(home: Path) -> None:
    for harness in profile_ids():
        done = arbora(
            "plugin",
            "bundle",
            "--harness",
            harness,
            "--endpoint",
            ENDPOINT,
            "--print-path",
        )
        assert done.returncode == 0, done.stderr
        tree = Path(done.stdout.strip())
        assert validate_tree(harness, tree) == [], harness


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R002.5")
def test_install_change_status_remove(home: Path, skill_root: Path) -> None:
    bin_dir = home / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(f'#!/bin/sh\necho "$@" >> {home}/claude-calls.txt\n')
    claude.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    before = {p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()}
    for harness in ("claude-code", "opencode"):
        done = arbora(
            "plugin", "install", "--harness", harness, "--endpoint", ENDPOINT, env=env
        )
        assert done.returncode == 0, done.stderr
    write_skill(skill_root, "canopy", "The door skill changed.")
    status = arbora(
        "plugin", "status", "--harness", "claude-code", "--harness", "opencode", env=env
    )
    states = {k: v["state"] for k, v in json.loads(status.stdout).items()}
    assert states == {"claude-code": "stale", "opencode": "stale"}
    for harness in ("claude-code", "opencode"):
        assert arbora("plugin", "remove", "--harness", harness, env=env).returncode == 0
    calls = (home / "claude-calls.txt").read_text().splitlines()
    assert calls[-2:] == [
        "plugin uninstall arbora@arbora-local",
        "plugin marketplace remove arbora-local",
    ]
    after = {
        p.relative_to(home).as_posix()
        for p in home.rglob("*")
        if p.is_file()
        and not p.relative_to(home).as_posix().startswith((".cache/", "bin/"))
    }
    assert after - before == {"claude-calls.txt", ".config/arbora/plugins.json"}
