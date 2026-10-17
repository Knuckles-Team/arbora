"""Suite-wide isolation: no test reads the host's own configuration files."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every configuration lookup at an empty per-test directory."""
    home = tmp_path / "config-home"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("GRAPH_OS_CONFIG_DIR", str(home / "graph-os"))
    monkeypatch.delenv("AGENT_UTILITIES_CONFIG_DIR", raising=False)
