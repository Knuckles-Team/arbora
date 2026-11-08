"""The opencode renderer and its generated module (ARBORA-HARNESS-PLUGINS-R006)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.profiles import profile
from arbora.harness_plugins.render import render
from tests.harness_plugins.conftest import ENDPOINT

LOADER = Path(__file__).parent / "fixtures" / "opencode_loader.mjs"


def _tree(skill_root: Path, tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    render(
        compose_bundle(
            profile("opencode"), base_url=ENDPOINT, skill_roots=[skill_root]
        ),
        tree,
    )
    return tree


def _run_loader(tree: Path, tmp_path: Path) -> tuple[dict, list[dict]]:
    if shutil.which("node") is None:
        pytest.fail("node is required to run the generated opencode module")
    calls = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "arbora"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'input': json.load(sys.stdin)}}) + '\\n')\n"
        "print(json.dumps({'context': 'handoff from claude-code'}) if sys.argv[3] == 'first_prompt' else '')\n"
    )
    fake.chmod(0o755)
    out = tmp_path / "record.json"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--no-warnings",
            str(LOADER),
            str(tree),
            str(out),
        ],
        check=True,
        env=env,
        timeout=60,
    )
    lines = calls.read_text().splitlines() if calls.exists() else []
    return json.loads(out.read_text()), [json.loads(line) for line in lines]


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R006.1")
def test_layout_matches_the_profile(skill_root: Path, tmp_path: Path) -> None:
    tree = _tree(skill_root, tmp_path)
    files = sorted(
        p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()
    )
    assert files == [
        "bundle.json",
        "package.json",
        "server.ts",
        "skills/canopy/SKILL.md",
        "skills/session-handoff/SKILL.md",
    ]
    package = json.loads((tree / "package.json").read_text())
    assert package["main"] == "server.ts" and package["type"] == "module"
    template = (
        Path(__file__).resolve().parents[2]
        / "arbora/harness_plugins/templates/opencode/server.ts"
    ).read_text()
    assert (tree / "server.ts").read_text() == template
    assert "example.test" not in template and "http" not in template.split("import")[0]


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R006.2")
def test_setup_registers_door_skills_and_commands(
    skill_root: Path, tmp_path: Path
) -> None:
    record, _ = _run_loader(_tree(skill_root, tmp_path), tmp_path)
    assert record["mcp"] == {
        "arbora": {
            "type": "remote",
            "url": ENDPOINT + "/mcp",
            "oauth": {
                "clientId": "arbora-native-mcp",
                "redirectUri": "http://127.0.0.1:19876/mcp/oauth/callback",
            },
        }
    }
    assert [s["id"] for s in record["skills"]] == ["canopy", "session-handoff"]
    assert all(s["hasContent"] and s["description"] for s in record["skills"])
    assert [c["name"] for c in record["commands"]] == ["handoff", "resume"]


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R006.3")
def test_hooks_call_workstream_operations_non_blocking(
    skill_root: Path, tmp_path: Path
) -> None:
    record, calls = _run_loader(_tree(skill_root, tmp_path), tmp_path)
    assert record["hooks"] == ["context", "compaction"]
    assert record["events"] == ["session.deleted"]
    assert [c["argv"] for c in calls] == [
        ["plugin", "hook", point, "--harness", "opencode"]
        for point in ("first_prompt", "before_compaction", "session_end")
    ]
    assert calls[0]["input"] == {"session_id": "oc-1", "cwd": "/workspace/project"}
    assert record["system"] == [{"type": "text", "text": "handoff from claude-code"}]


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R006.4")
def test_loader_fixture_runs_the_generated_plugin(
    skill_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _tree(skill_root, tmp_path)
    broken = tmp_path / "broken-bin"
    broken.mkdir()
    (broken / "arbora").write_text("#!/bin/sh\nexit 3\n")
    (broken / "arbora").chmod(0o755)
    out = tmp_path / "broken.json"
    env = {**os.environ, "PATH": f"{broken}{os.pathsep}{os.environ['PATH']}"}
    subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "--no-warnings",
            str(LOADER),
            str(tree),
            str(out),
        ],
        check=True,
        env=env,
        timeout=60,
    )
    assert json.loads(out.read_text())["system"] == []
