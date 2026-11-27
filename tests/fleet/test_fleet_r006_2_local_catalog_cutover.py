"""ARBORA-FLEET-R006.2: skill/prompt body-harvest cutover for an
already-admitted child.

``_probe_skills``/``_probe_prompts`` (``arbora/fleet/multiplexer.py``) now
resolve a resource's body from the resident local-skill catalog
(``arbora.fleet.local_skill_catalog.build_local_skill_catalog``) when its
name is already admitted there, with no read of the child's own session. A
name the local catalog does not admit carries no body (ARBORA-FLEET-R006.3.2
removed the live fallback) -- this file proves both halves on one fixture
child.
"""

from __future__ import annotations

import pytest

import arbora.fleet.fleet_probe as _fleet_probe
from tests.fleet.test_multiplexer_dynamic_gateway import (
    CNT,
    CNT_TOOL,
    _mux_with_children,
)
from tests.fleet.test_multiplexer_skills_over_mcp import (
    _fake_session_with_resources,
    _fake_skill_resource,
)


def _fixture_local_catalog(entries):
    return lambda: (entries, [])


@pytest.mark.spec("ARBORA-FLEET-R006.2")
async def test_admitted_skill_resolves_via_local_catalog_without_harvest(
    tmp_path, monkeypatch
):
    """A skill name the local catalog already admits is filled in from that
    catalog's body -- the mounted child's own ``read_resource`` is never
    called for it."""
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage containers")]})
    session = _fake_session_with_resources(
        [(CNT_TOOL, "manage containers")],
        [_fake_skill_resource("skill://admitted-skill/SKILL.md", "fleet copy")],
        bodies={"skill://admitted-skill/SKILL.md": "fleet-harvested body"},
    )
    monkeypatch.setattr(
        _fleet_probe,
        "build_local_skill_catalog",
        _fixture_local_catalog(
            [
                {
                    "name": "admitted-skill",
                    "uri": "skill://admitted-skill/SKILL.md",
                    "description": "resident copy",
                    "instructions": "resident local-catalog body",
                }
            ]
        ),
    )

    skills = await mux.probe._probe_skills(CNT, session)

    assert len(skills) == 1
    assert skills[0]["instructions"] == "resident local-catalog body"
    session.read_resource.assert_not_called()


@pytest.mark.spec("ARBORA-FLEET-R006.2")
@pytest.mark.spec("ARBORA-FLEET-R006.3.2")
async def test_non_admitted_skill_has_no_body_and_no_wire_read(tmp_path, monkeypatch):
    """A skill name the local catalog does NOT resolve keeps no body, and the
    mounted child is never read."""
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage containers")]})
    session = _fake_session_with_resources(
        [(CNT_TOOL, "manage containers")],
        [_fake_skill_resource("skill://unlisted-skill/SKILL.md", "fleet-only")],
        bodies={"skill://unlisted-skill/SKILL.md": "child body never read"},
    )
    monkeypatch.setattr(
        _fleet_probe, "build_local_skill_catalog", _fixture_local_catalog([])
    )

    skills = await mux.probe._probe_skills(CNT, session)

    assert len(skills) == 1
    assert "instructions" not in skills[0]
    session.read_resource.assert_not_called()


@pytest.mark.spec("ARBORA-FLEET-R006.2")
async def test_admitted_prompt_resolves_via_local_catalog_without_harvest(
    tmp_path, monkeypatch
):
    """The same cutover applies to ``_probe_prompts``: a prompt name the
    local catalog admits skips the live read entirely."""
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage containers")]})
    session = _fake_session_with_resources(
        [(CNT_TOOL, "manage containers")],
        [_fake_skill_resource("prompt://provider/admitted-prompt", "fleet copy")],
        bodies={"prompt://provider/admitted-prompt": "fleet-harvested prompt body"},
    )
    monkeypatch.setattr(
        _fleet_probe,
        "build_local_skill_catalog",
        _fixture_local_catalog(
            [
                {
                    "name": "admitted-prompt",
                    "uri": "prompt://provider/admitted-prompt",
                    "description": "resident copy",
                    "instructions": "resident local-catalog prompt body",
                }
            ]
        ),
    )

    prompts = await mux.probe._probe_prompts(CNT, session)

    assert len(prompts) == 1
    assert prompts[0]["body"] == "resident local-catalog prompt body"
    session.read_resource.assert_not_called()


@pytest.mark.spec("ARBORA-FLEET-R006.2")
@pytest.mark.spec("ARBORA-FLEET-R006.3.2")
async def test_non_admitted_prompt_has_no_body_and_no_wire_read(tmp_path, monkeypatch):
    mux = _mux_with_children(tmp_path, {CNT: [(CNT_TOOL, "manage containers")]})
    session = _fake_session_with_resources(
        [(CNT_TOOL, "manage containers")],
        [_fake_skill_resource("prompt://provider/unlisted-prompt", "fleet-only")],
        bodies={"prompt://provider/unlisted-prompt": "child body never read"},
    )
    monkeypatch.setattr(
        _fleet_probe, "build_local_skill_catalog", _fixture_local_catalog([])
    )

    prompts = await mux.probe._probe_prompts(CNT, session)

    assert len(prompts) == 1
    assert "body" not in prompts[0]
    session.read_resource.assert_not_called()
