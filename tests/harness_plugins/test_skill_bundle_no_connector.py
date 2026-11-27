"""A skill bundle never carries its own connector file (R015)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arbora.core.harness_profiles import PROFILES, profile
from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.render import TreeRefused, render
from tests.harness_plugins.conftest import ENDPOINT

pytestmark = pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R015")


def _bundle(skill_root: Path, prof_id: str = "claude-code") -> object:
    return compose_bundle(profile(prof_id), base_url=ENDPOINT, skill_roots=[skill_root])


@pytest.mark.parametrize(
    ("name", "text"),
    [
        (".mcp.json", "{}"),
        ("mcp.json", "{}"),
        ("config/mcp_config.json", "{}"),
        ("settings.json", '{"mcpServers": {"x": {"url": "http://h"}}}'),
    ],
)
def test_mcp_json_inside_a_skill_refuses_the_bundle(
    skill_root: Path, tmp_path: Path, name: str, text: str
) -> None:
    extra = skill_root / "canopy" / name
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text(text)
    for prof in PROFILES:
        out = tmp_path / prof.id
        with pytest.raises(TreeRefused) as refused:
            render(_bundle(skill_root, prof.id), out)  # type: ignore[arg-type]
        assert refused.value.code == "SKILL_BUNDLE_HAS_CONNECTOR_FILE"
        assert not (out / (prof.manifest or "playbook.md")).exists()


def test_a_clean_skill_still_renders(skill_root: Path, tmp_path: Path) -> None:
    (skill_root / "canopy" / "notes.json").write_text('{"topic": "door"}')
    digest = render(_bundle(skill_root), tmp_path / "ok")  # type: ignore[arg-type]
    assert len(digest) == 64
