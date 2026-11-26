"""The identity runbook names only commands, settings and scripts that exist (ARBORA-IDENTITY-R015)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (
    ROOT / "docs" / "identity.md",
    ROOT
    / "arbora"
    / "skills"
    / "arbora-deployment"
    / "references"
    / "identity-and-access.md",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _source() -> str:
    return "\n".join(
        _text(path)
        for path in (ROOT / "arbora").rglob("*.py")
        if "skills" not in path.parts
    )


@pytest.mark.spec("ARBORA-IDENTITY-R015")
def test_documented_commands_and_settings_exist() -> None:
    scripts = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]["scripts"]
    source = _source()
    from arbora.identity.cli import COMMANDS

    for path in DOCUMENTS:
        text = _text(path)
        for command in set(re.findall(r"`arbora-identity ([a-z-]+)", text)):
            assert command in COMMANDS, (path.name, command)
        for entry in set(
            re.findall(r"(?:run|use|with) `(arbora-[a-z-]+|setup-config)[ `]", text)
        ):
            assert entry in scripts, (path.name, entry)
        for name in set(re.findall(r"`(ARBORA_(?:IDENTITY|SETUP)_[A-Z_]+)`", text)):
            assert name in source, (path.name, name)
        for script in set(re.findall(r"`(scripts/[a-z_]+\.py)`", text)):
            assert (ROOT / script).is_file(), (path.name, script)
    assert "arbora-identity" in scripts


@pytest.mark.spec("ARBORA-IDENTITY-R015")
def test_the_runbook_names_the_identity_probe_script() -> None:
    combined = "\n".join(_text(path) for path in DOCUMENTS)
    assert "ARBORA_SETUP_CODE" in combined
    assert (ROOT / "scripts" / "identity_probe.py").is_file()
