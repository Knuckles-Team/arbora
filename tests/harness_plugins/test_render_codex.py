"""The codex renderer (ARBORA-HARNESS-PLUGINS-R008)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.profiles import profile
from arbora.harness_plugins.render import render
from tests.harness_plugins.conftest import ENDPOINT


def _tree(harness: str, skill_root: Path, tmp_path: Path) -> Path:
    tree = tmp_path / harness
    render(
        compose_bundle(profile(harness), base_url=ENDPOINT, skill_roots=[skill_root]),
        tree,
    )
    return tree


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R008")
def test_layout_hooks_and_mcp_entry(skill_root: Path, tmp_path: Path) -> None:
    tree = _tree("codex", skill_root, tmp_path)
    assert (tree / ".codex-plugin/plugin.json").is_file()
    server = json.loads((tree / ".mcp.json").read_text())["mcpServers"]["arbora"]
    assert server == {
        "type": "http",
        "url": ENDPOINT + "/mcp",
        "oauth": {"clientId": "arbora-native-mcp"},
    }
    hooks = json.loads((tree / "hooks/hooks.json").read_text())["hooks"]
    assert sorted(hooks) == ["PreCompact", "UserPromptSubmit"]
    for event, point in (
        ("UserPromptSubmit", "first_prompt"),
        ("PreCompact", "before_compaction"),
    ):
        (handler,) = hooks[event][0]["hooks"]
        assert handler == {
            "type": "command",
            "command": f"arbora plugin hook {point} --harness codex",
            "timeout": 10,
        }
