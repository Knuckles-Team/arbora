"""Fixtures of the harness plugin tests: a loopback home and fixture skills."""

from __future__ import annotations

from pathlib import Path

import pytest

ENDPOINT = "https://door.example.test"


def write_skill(root: Path, name: str, body: str = "Use the door.") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n")
    return path


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "content" / "skills"
    for name in ("arbora-door", "session-handoff", "extra-skill"):
        write_skill(root, name)
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_root: Path) -> Path:
    """An isolated home: config, cache and harness dirs all under ``tmp_path``."""
    root = tmp_path / "home"
    root.mkdir()
    for key, rel in {
        "HOME": "",
        "XDG_CONFIG_HOME": ".config",
        "XDG_CACHE_HOME": ".cache",
        "XDG_DATA_HOME": ".local/share",
        "XDG_STATE_HOME": ".local/state",
    }.items():
        monkeypatch.setenv(key, str(root / rel) if rel else str(root))
    monkeypatch.delenv("ARBORA_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ARBORA_PUBLIC_URL", raising=False)
    monkeypatch.setenv("ARBORA_PLUGIN_SKILL_ROOTS", str(skill_root))
    monkeypatch.setenv("ARBORA_PLUGIN_FILE_STORE", "1")
    monkeypatch.chdir(root)
    return root
