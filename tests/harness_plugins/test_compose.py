"""The harness-neutral composer (ARBORA-HARNESS-PLUGINS-R001.2, R003.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from arbora.harness_plugins.compose import (
    DOOR_SKILLS,
    NATIVE_CLIENT_ID,
    AgentDef,
    SkillMissing,
    compose_bundle,
)
from arbora.harness_plugins.profiles import Lifecycle, profile
from tests.harness_plugins.conftest import ENDPOINT


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R001.2")
def test_bundle_holds_door_skills_agents_and_no_prompt_copy(skill_root: Path) -> None:
    agents = [AgentDef("reviewer", "Reviews a diff", "code-reviewer")]
    spec = compose_bundle(
        profile("claude-code"),
        base_url=ENDPOINT,
        skills=["extra-skill"],
        agents=agents,
        skill_roots=[skill_root],
    )
    assert [s.name for s in spec.skills] == sorted([*DOOR_SKILLS, "extra-skill"])
    assert spec.agents == tuple(agents)
    assert {c.name for c in spec.commands} == {"handoff", "resume"}
    assert [h.op for h in spec.hooks] == [
        "workstream.attach",
        "workstream.checkpoint",
        "workstream.detach",
    ]
    assert not hasattr(spec, "prompts")
    default = compose_bundle(
        profile("claude-code"), base_url=ENDPOINT, skill_roots=[skill_root]
    )
    assert [s.name for s in default.skills] == sorted(DOOR_SKILLS)
    codex = compose_bundle(
        profile("codex"), base_url=ENDPOINT, skill_roots=[skill_root]
    )
    assert Lifecycle.SESSION_END not in {h.point for h in codex.hooks}
    with pytest.raises(SkillMissing, match="nope"):
        compose_bundle(
            profile("opencode"),
            base_url=ENDPOINT,
            skills=["nope"],
            skill_roots=[skill_root],
        )


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R003.1")
def test_mcp_entry_names_native_client_and_profile_callback(skill_root: Path) -> None:
    for harness in ("claude-code", "opencode", "codex", "grok"):
        spec = compose_bundle(
            profile(harness), base_url=ENDPOINT + "/", skill_roots=[skill_root]
        )
        assert spec.door.url == ENDPOINT + "/mcp"
        assert spec.door.client_id == NATIVE_CLIENT_ID
        assert spec.door.callback == profile(harness).callback
        assert spec.door.callback_port == profile(harness).callback_port
        assert not any("token" in repr(v).lower() for v in vars(spec.door).values())
