"""Arbora configuration location and the one-release legacy fallback."""

from __future__ import annotations

import warnings

import pytest

import arbora
from arbora.core import settings

PREVIOUS_DIR_SETTING = "GRAPH_OS_CONFIG_DIR"  # arbora-rename: keep
PREVIOUS_APP_DIR = "graph-os"  # arbora-rename: keep


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in (
        "ARBORA_CONFIG_DIR",
        PREVIOUS_DIR_SETTING,
        "AGENT_UTILITIES_CONFIG_DIR",
    ):
        monkeypatch.setenv(name, "")  # records the prior value for undo
        monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(settings, "_warned_legacy", set())
    return tmp_path


def _write(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


def test_default_location_is_arbora(home) -> None:
    assert (
        settings.platform_config_file("mcp_config.json")
        == home / "arbora" / "mcp_config.json"
    )


def test_explicit_directory_wins_even_when_legacy_exists(home, monkeypatch) -> None:
    _write(home / "agent-utilities" / "mcp_config.json")
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(home / "explicit"))
    assert (
        settings.platform_config_file("mcp_config.json")
        == home / "explicit" / "mcp_config.json"
    )


@pytest.mark.parametrize("legacy_env", [False, True])
def test_legacy_file_is_found_with_one_warning(home, monkeypatch, legacy_env) -> None:
    legacy_dir = home / ("old" if legacy_env else "agent-utilities")
    if legacy_env:
        monkeypatch.setenv("AGENT_UTILITIES_CONFIG_DIR", str(legacy_dir))
    legacy = _write(legacy_dir / "mcp_config.json")
    with pytest.warns(DeprecationWarning):
        assert settings.platform_config_file("mcp_config.json") == legacy
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert settings.platform_config_file("mcp_config.json") == legacy


@pytest.mark.parametrize("legacy_env", [False, True])
def test_previous_project_location_is_read_first(home, monkeypatch, legacy_env) -> None:
    previous_dir = home / ("previous" if legacy_env else PREVIOUS_APP_DIR)
    if legacy_env:
        monkeypatch.setenv(PREVIOUS_DIR_SETTING, str(previous_dir))
    _write(home / "agent-utilities" / "mcp_config.json")
    previous = _write(previous_dir / "mcp_config.json")
    with pytest.warns(DeprecationWarning):
        assert settings.platform_config_file("mcp_config.json") == previous


def test_preferred_file_beats_legacy(home) -> None:
    _write(home / "agent-utilities" / "mcp_config.json")
    preferred = _write(home / "arbora" / "mcp_config.json")
    assert settings.platform_config_file("mcp_config.json") == preferred


@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [(None, 3, 3), ("7", 3, 7), ("bad", 3, 3), ("true", False, True)],
)
def test_setting_reads_environment_typed_by_default(
    monkeypatch, raw, default, expected
) -> None:
    if raw is None:
        monkeypatch.delenv("GOS_TEST_SETTING", raising=False)
    else:
        monkeypatch.setenv("GOS_TEST_SETTING", raw)
    assert settings.setting("GOS_TEST_SETTING", default) == expected


def test_bridge_points_shared_helpers_at_the_arbora_directory(
    home, monkeypatch
) -> None:
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(home / "explicit"))
    arbora._bridge_shared_config_dir()
    import os

    assert os.environ[PREVIOUS_DIR_SETTING] == str(home / "explicit")


def test_bridge_leaves_shared_helpers_alone_without_an_arbora_directory(
    home,
) -> None:
    import os

    arbora._bridge_shared_config_dir()
    assert PREVIOUS_DIR_SETTING not in os.environ


def test_bridge_never_overrides_an_explicit_shared_directory(home, monkeypatch) -> None:
    monkeypatch.setenv(PREVIOUS_DIR_SETTING, str(home / "kept"))
    _write(home / "arbora" / "config.json")
    arbora._bridge_shared_config_dir()
    import os

    assert os.environ[PREVIOUS_DIR_SETTING] == str(home / "kept")
