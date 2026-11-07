"""The claude-code renderer (ARBORA-HARNESS-PLUGINS-R005)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from arbora.harness_plugins import cli
from arbora.harness_plugins import install as inst
from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.profiles import profile
from arbora.harness_plugins.render import render
from tests.harness_plugins.conftest import ENDPOINT


def _tree(skill_root: Path, tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    render(
        compose_bundle(
            profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
        ),
        tree,
    )
    return tree


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R005.1")
def test_layout_matches_the_profile(skill_root: Path, tmp_path: Path) -> None:
    tree = _tree(skill_root, tmp_path)
    files = sorted(
        p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()
    )
    assert files == [
        ".claude-plugin/plugin.json",
        ".mcp.json",
        "commands/handoff.md",
        "commands/resume.md",
        "hooks/hooks.json",
        "skills/arbora-door/SKILL.md",
        "skills/session-handoff/SKILL.md",
    ]
    manifest = json.loads((tree / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "arbora" and "+" in manifest["version"]
    server = json.loads((tree / ".mcp.json").read_text())["mcpServers"]["arbora"]
    assert server == {
        "type": "http",
        "url": ENDPOINT + "/mcp",
        "oauth": {"clientId": "arbora-native-mcp", "callbackPort": 33418},
    }


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R005.2")
def test_hooks_are_mcp_tool_calls_to_the_door(skill_root: Path, tmp_path: Path) -> None:
    hooks = json.loads((_tree(skill_root, tmp_path) / "hooks/hooks.json").read_text())[
        "hooks"
    ]
    assert sorted(hooks) == ["PreCompact", "SessionEnd", "UserPromptSubmit"]
    ops = {}
    for event, groups in hooks.items():
        (handler,) = groups[0]["hooks"]
        assert handler["type"] == "mcp_tool"
        assert (
            handler["server"] == "plugin:arbora:arbora" and handler["tool"] == "write"
        )
        assert "command" not in handler
        ops[event] = handler["input"]
    assert ops["UserPromptSubmit"]["op"] == "workstream.attach"
    assert ops["UserPromptSubmit"]["params"] == {
        "harness": "claude-code",
        "harness_session_id": "${session_id}",
        "workspace_root": "${cwd}",
    }
    assert ops["PreCompact"]["op"] == "workstream.checkpoint"
    assert ops["PreCompact"]["params"]["trigger"] == "compact"
    assert ops["SessionEnd"]["op"] == "workstream.detach"
    assert all(i["execute"] is True for i in ops.values())


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R005.3")
def test_marketplace_entry_uses_the_bundle_command(home: Path) -> None:
    calls: list[list[str]] = []
    record = inst.InstallRecord("claude-code", "d", "", inst.InstallState.GENERATED)
    files = inst.place_claude_code(record, lambda argv: calls.append(list(argv)) or 0)
    doc = json.loads(Path(files[0]).read_text())
    (entry,) = doc["plugins"]
    assert doc["name"] == "arbora-local" and entry["name"] == "arbora"
    assert entry["source"] == {
        "source": "command",
        "command": "arbora plugin bundle --harness claude-code --print-path",
        "mode": "copy",
        "timeout": 120,
    }
    assert calls == [
        ["claude", "plugin", "marketplace", "add", record.place],
        ["claude", "plugin", "install", "arbora@arbora-local"],
    ]


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R005.4")
def test_statusline_prints_one_line_quickly(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    started = time.monotonic()
    assert cli.main(["statusline", "--harness", "claude-code"]) == 0
    assert time.monotonic() - started < 0.2
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("arbora absent")
