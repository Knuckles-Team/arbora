"""Deterministic trees with no secret and no escaping path (R001.3, R001.6)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arbora.harness_plugins.compose import compose_bundle
from arbora.harness_plugins.profiles import PROFILES, profile
from arbora.harness_plugins.render import TreeRefused, check_tree, render, tree_digest
from tests.harness_plugins.conftest import ENDPOINT, write_skill


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R001.3")
def test_two_renders_give_one_digest(skill_root: Path, tmp_path: Path) -> None:
    for prof in PROFILES:
        spec = compose_bundle(prof, base_url=ENDPOINT, skill_roots=[skill_root])
        first = render(spec, tmp_path / prof.id / "a")
        second = render(spec, tmp_path / prof.id / "b")
        assert first == second
        assert first == tree_digest(tmp_path / prof.id / "a", exclude=(prof.manifest,))
        manifest = (tmp_path / prof.id / "a" / prof.manifest).read_text()
        assert first[:12] in manifest
    write_skill(skill_root, "canopy", "Changed text.")
    changed = render(
        compose_bundle(
            profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
        ),
        tmp_path / "c",
    )
    assert changed != tree_digest(
        tmp_path / "claude-code" / "a", exclude=(".claude-plugin/plugin.json",)
    )


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R001.6")
def test_bundle_holds_no_secret_and_no_escaping_path(
    skill_root: Path, tmp_path: Path
) -> None:
    tree = tmp_path / "tree"
    render(
        compose_bundle(
            profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
        ),
        tree,
    )
    check_tree(tree, allowed_urls=(ENDPOINT + "/mcp",))
    text = "".join(p.read_text() for p in tree.rglob("*") if p.is_file())
    assert "token" not in text.lower().replace("refresh token", "")
    write_skill(skill_root, "canopy", 'key "client_secret": "abc"')
    with pytest.raises(TreeRefused, match="SECRET_IN_BUNDLE"):
        render(
            compose_bundle(
                profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
            ),
            tmp_path / "s",
        )
    write_skill(skill_root, "canopy", "Read /opt/elsewhere/notes.txt first.")
    with pytest.raises(TreeRefused, match="PATH_ESCAPES_TREE"):
        render(
            compose_bundle(
                profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
            ),
            tmp_path / "p",
        )
    write_skill(skill_root, "canopy")
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    os.symlink(outside, skill_root / "canopy" / "linked.md")
    with pytest.raises(TreeRefused, match="PATH_ESCAPES_TREE"):
        render(
            compose_bundle(
                profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
            ),
            tmp_path / "l",
        )
