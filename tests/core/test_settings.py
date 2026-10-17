"""Graph OS configuration location and the one-release legacy fallback."""

from __future__ import annotations

import warnings

import pytest

from graph_os.core import settings


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("GRAPH_OS_CONFIG_DIR", "AGENT_UTILITIES_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(settings, "_warned_legacy", set())
    return tmp_path


def _write(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    return path


def test_default_location_is_graph_os(home) -> None:
    assert (
        settings.platform_config_file("mcp_config.json")
        == home / "graph-os" / "mcp_config.json"
    )


def test_explicit_directory_wins_even_when_legacy_exists(home, monkeypatch) -> None:
    _write(home / "agent-utilities" / "mcp_config.json")
    monkeypatch.setenv("GRAPH_OS_CONFIG_DIR", str(home / "explicit"))
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


def test_preferred_file_beats_legacy(home) -> None:
    _write(home / "agent-utilities" / "mcp_config.json")
    preferred = _write(home / "graph-os" / "mcp_config.json")
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
