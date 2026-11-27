"""ARBORA-HOST-R007.2.38: ``arbora.core.settings`` owns the data directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from arbora.core.settings import data_dir


@pytest.mark.spec("ARBORA-HOST-R007.2.38")
def test_data_dir_env_override_and_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARBORA_DATA_DIR", str(tmp_path / "explicit"))
    assert data_dir() == tmp_path / "explicit"
    monkeypatch.delenv("ARBORA_DATA_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert data_dir() == tmp_path / "xdg" / "arbora"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert data_dir() == tmp_path / ".local" / "share" / "arbora"
