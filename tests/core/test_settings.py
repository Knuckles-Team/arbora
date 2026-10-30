"""Arbora configuration location: ``ARBORA_CONFIG_DIR`` or ``~/.config/arbora`` only."""

from __future__ import annotations

import warnings

import pytest

from arbora.core import settings

RETIRED_DIR_SETTINGS = ("GRAPH_OS_CONFIG_DIR", "AGENT_UTILITIES_CONFIG_DIR")
RETIRED_APP_DIRS = ("graph-os", "agent-utilities")


@pytest.fixture
def home(tmp_path, monkeypatch):
    for name in ("ARBORA_CONFIG_DIR", *RETIRED_DIR_SETTINGS):
        monkeypatch.setenv(name, "")  # records the prior value for undo
        monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
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


def test_mcp_path_uses_active_directory(home, monkeypatch) -> None:
    assert settings.mcp_config_path() == home / "arbora" / "mcp_config.json"
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(home / "explicit"))
    assert settings.mcp_config_path() == home / "explicit" / "mcp_config.json"
    monkeypatch.setenv("ARBORA_CONFIG_DIR", f"  {home / 'explicit'}  ")
    assert settings.mcp_config_path() == home / "explicit" / "mcp_config.json"


def test_explicit_directory_wins(home, monkeypatch) -> None:
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(home / "explicit"))
    assert (
        settings.platform_config_file("mcp_config.json")
        == home / "explicit" / "mcp_config.json"
    )


@pytest.mark.parametrize("use_env", [False, True])
def test_retired_locations_are_not_read(home, monkeypatch, use_env) -> None:
    """A file present only under a retired directory is neither found nor warned about."""
    for app_dir, variable in zip(RETIRED_APP_DIRS, RETIRED_DIR_SETTINGS, strict=True):
        directory = home / (f"{app_dir}-env" if use_env else app_dir)
        if use_env:
            monkeypatch.setenv(variable, str(directory))
        _write(directory / "mcp_config.json")
        _write(directory / "workspace.yml")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert (
            settings.platform_config_file("workspace.yml")
            == home / "arbora" / "workspace.yml"
        )
        assert settings.config_dir() == home / "arbora"


def test_preferred_file_is_returned_when_present(home) -> None:
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


@pytest.mark.parametrize(
    ("alias", "text", "field", "expected"),
    [
        (
            "MCP_ALWAYS_LOAD",
            '["graph", "fleet"]',
            "mcp_always_load",
            ["graph", "fleet"],
        ),
        ("MCP_ALWAYS_LOAD", "graph, fleet", "mcp_always_load", ["graph", "fleet"]),
        (
            "GRAPH_SERVICE_ENDPOINTS",
            '["unix:///run/eg.sock"]',
            "graph_service_endpoints",
            ["unix:///run/eg.sock"],
        ),
        (
            "EXTRA_HEADERS",
            '{"X-Tenant": "boot"}',
            "extra_headers",
            {"X-Tenant": "boot"},
        ),
        (
            "MCP_FLEET_SECRET_REFS",
            '{"fleet": "env://FLEET_TOKEN"}',
            "mcp_fleet_secret_refs",
            {"fleet": "env://FLEET_TOKEN"},
        ),
    ],
)
def test_config_accepts_json_text_for_structured_fields(
    monkeypatch, alias, text, field, expected
) -> None:
    """The k8s config stores lists and mappings as JSON text in the environment."""
    monkeypatch.setenv(alias, text)

    assert getattr(settings.ArboraConfig(), field) == expected
    assert getattr(settings.runtime_config, field) == expected
    assert getattr(settings.ArboraConfig(**{alias: text}), field) == expected


def test_config_document_round_trips_structured_values(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        '{"MCP_ALWAYS_LOAD": "[\\"graph\\"]", "EXTRA_HEADERS": "{\\"X\\": \\"1\\"}"}'
    )

    cfg = settings.config_from_document(settings.read_config_document(path))

    assert cfg.mcp_always_load == ["graph"]
    assert cfg.extra_headers == {"X": "1"}


def test_save_config_item_writes_structured_values_as_json_text(
    home, monkeypatch
) -> None:
    import json

    # save_config_item also sets os.environ; record the key for undo.
    monkeypatch.setenv("MCP_ALWAYS_LOAD", "")
    settings.save_config_item("MCP_ALWAYS_LOAD", ["graph", "fleet"])
    path = home / "arbora" / "config.json"

    assert json.loads(path.read_text())["MCP_ALWAYS_LOAD"] == '["graph","fleet"]'
    assert settings.read_config_document(path)["MCP_ALWAYS_LOAD"] == '["graph","fleet"]'
    assert settings.ArboraConfig().mcp_always_load == ["graph", "fleet"]
