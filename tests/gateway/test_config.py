"""Tests for arbora.gateway.config — migrated from service-dashboard-core."""

import json

import pytest

from arbora.gateway.config import ConfigManager
from arbora.gateway.models import (
    DashboardLayout,
    ServiceCategory,
    ServiceConfig,
    ServiceGroup,
)


def test_config_manager_load_save(tmp_path):
    config_file = tmp_path / "services.yaml"
    mgr = ConfigManager(config_path=config_file)

    # Check loading when file doesn't exist (returns empty layout or runs auto-discover)
    layout = mgr.load()
    assert isinstance(layout, DashboardLayout)

    # Save a dummy layout
    services = [
        ServiceConfig(
            id="test-service",
            name="Test Service",
            widget_type="portainer",
            url="http://localhost:9000",
            category=ServiceCategory.INFRASTRUCTURE,
        )
    ]
    group = ServiceGroup(name="Infrastructure", services=services)
    layout = DashboardLayout(groups=[group])

    mgr.save(layout)
    assert config_file.exists()

    # Load it back
    loaded_layout = mgr.load()
    assert len(loaded_layout.groups) == 1
    assert loaded_layout.groups[0].name == "Infrastructure"
    assert loaded_layout.groups[0].services[0].name == "Test Service"
    assert loaded_layout.groups[0].services[0].id == "test-service"


def test_auto_discover(tmp_path, monkeypatch):
    mcp_config = tmp_path / "mcp_config.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "portainer-agent": {
                        "command": "uv",
                        "args": ["run", "portainer-agent"],
                        "env": {"PORTAINER_URL": "http://portainer.local"},
                    }
                }
            }
        )
    )

    # Patch path functions to use tmp_path
    monkeypatch.setattr("arbora.gateway.config.mcp_config_path", lambda: mcp_config)
    monkeypatch.setattr(
        "arbora.gateway.config.services_config_path",
        lambda: tmp_path / "services.yaml",
    )

    mgr = ConfigManager(config_path=tmp_path / "services.yaml")
    layout = mgr.load()

    assert len(layout.groups) == 1
    assert layout.groups[0].name == ServiceCategory.INFRASTRUCTURE.value
    assert layout.groups[0].services[0].id == "portainer-agent"
    assert layout.groups[0].services[0].url == "http://portainer.local"


def test_dashboard_discovers_from_active_arbora_config(tmp_path, monkeypatch):
    active = tmp_path / "active"
    active.mkdir()
    (active / "mcp_config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "portainer-agent": {
                        "command": "uv",
                        "env": {"PORTAINER_URL": "https://portainer.example.test"},
                    }
                }
            }
        )
    )
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(active))
    monkeypatch.setenv("MERISTEM_CONFIG_DIR", str(tmp_path / "legacy"))

    layout = ConfigManager().load()

    assert layout.groups[0].services[0].id == "portainer-agent"
    assert layout.groups[0].services[0].url == "https://portainer.example.test"


def test_inline_credentials_are_never_serialized(tmp_path):
    config_file = tmp_path / "services.yaml"
    manager = ConfigManager(config_path=config_file)
    service = ServiceConfig(
        id="service",
        name="Service",
        widget_type="portainer",
        api_key="top-secret",  # sanitizer:ignore - synthetic serialization fixture, not a live credential
        username="private-user",
        password="private-password",  # sanitizer:ignore - synthetic serialization fixture, not a live credential
        credential_refs={"api_key": "env://SERVICE_API_KEY"},
    )
    manager.save(
        DashboardLayout(groups=[ServiceGroup(name="Services", services=[service])])
    )

    persisted = config_file.read_text(encoding="utf-8")
    for forbidden in ("top-secret", "private-user", "private-password"):
        assert forbidden not in persisted
    assert "env://SERVICE_API_KEY" in persisted
    assert "api_key" not in service.model_dump()


def test_persistent_inline_credentials_fail_closed(tmp_path):
    config_file = tmp_path / "services.yaml"
    config_file.write_text(
        """
groups:
  - name: Services
    services:
      - id: service
        name: Service
        widget_type: portainer
        api_key: top-secret
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inline credentials"):
        ConfigManager(config_path=config_file).load()


def test_credential_refs_require_runtime_secret_uris():
    with pytest.raises(ValueError, match="runtime secret reference"):
        ServiceConfig(
            id="service",
            name="Service",
            widget_type="portainer",
            credential_refs={"api_key": "top-secret"},
        )


@pytest.mark.spec("ARBORA-HOST-R007.2.38")
def test_layout_path_under_data_dir(monkeypatch, tmp_path) -> None:
    from arbora.gateway.config import dashboard_layout_path

    monkeypatch.setenv("ARBORA_DATA_DIR", str(tmp_path))
    assert dashboard_layout_path() == tmp_path / "layout.yaml"
