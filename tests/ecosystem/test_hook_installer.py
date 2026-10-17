"""Hook installation merges into an agent's settings and removes cleanly."""

from __future__ import annotations

import json

import pytest

from arbora.ecosystem import hook_installer as hi


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "_home", lambda: tmp_path)
    return tmp_path


def test_install_merges_existing_settings_and_uninstall_removes_the_hooks(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"theme": "dark"}))

    assert hi.install_hooks(["claude"]) == {"claude": "installed"}
    written = json.loads(settings.read_text())
    assert written["theme"] == "dark"
    assert written["hooks"]

    assert hi.uninstall_hooks(["claude"])["claude"] != "error"
    assert "theme" in json.loads(settings.read_text())


def test_unknown_agent_is_reported(home):
    assert hi.install_hooks(["no-such-agent"]) == {"no-such-agent": "unknown_agent"}
