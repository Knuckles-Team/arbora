"""The grok renderer (ARBORA-HARNESS-PLUGINS-R009)."""

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


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R009")
def test_layout_reuses_the_shared_parts(skill_root: Path, tmp_path: Path) -> None:
    grok = _tree("grok", skill_root, tmp_path)
    claude = _tree("claude-code", skill_root, tmp_path)
    for rel in (
        "skills/arbora-door/SKILL.md",
        "commands/handoff.md",
        "commands/resume.md",
    ):
        assert (grok / rel).read_bytes() == (claude / rel).read_bytes()
    market = json.loads((grok / ".grok-plugin/marketplace.json").read_text())
    assert market["plugins"] == [{"name": "arbora", "source": "./"}]
    hooks = json.loads((grok / "hooks/hooks.json").read_text())["hooks"]
    assert {h["type"] for g in hooks.values() for h in g[0]["hooks"]} == {"command"}
