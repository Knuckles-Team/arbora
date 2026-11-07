"""The closed harness profile table (ARBORA-HARNESS-PLUGINS-R001.1)."""

from __future__ import annotations

import pytest

from arbora.harness_plugins.profiles import PROFILES, HookKind, Lifecycle, Part, profile


@pytest.mark.spec("ARBORA-HARNESS-PLUGINS-R001.1")
def test_profile_table_is_closed_and_complete() -> None:
    assert [p.id for p in PROFILES] == ["claude-code", "opencode", "codex", "grok"]
    with pytest.raises(KeyError):
        profile("devin")
    for prof in PROFILES:
        assert prof.callback.startswith(("http://localhost:", "http://127.0.0.1:"))
        assert prof.callback_port > 1024
        assert prof.event(Lifecycle.FIRST_PROMPT) is not None
        assert {Part.SKILLS, Part.HOOKS, Part.MCP} <= prof.parts
        assert prof.door_configs and prof.skill_dirs
    assert len({p.callback for p in PROFILES}) == len(PROFILES)
    assert profile("claude-code").hook_kind is HookKind.MCP_TOOL
    assert {p.id for p in PROFILES if p.hook_kind is HookKind.COMMAND} == {
        "opencode",
        "codex",
        "grok",
    }
    assert profile("codex").event(Lifecycle.SESSION_END) is None
    from arbora.identity.client_contract import DEFAULT_NATIVE_REDIRECT_URIS

    for harness in ("claude-code", "opencode"):
        assert profile(harness).callback in DEFAULT_NATIVE_REDIRECT_URIS
