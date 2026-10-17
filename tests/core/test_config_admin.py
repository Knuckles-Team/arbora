"""ARBORA-DEPLOY-R009.1: Arbora-owned hosting/deployment settings module."""

from __future__ import annotations

import inspect

import pytest

from arbora.core import config_admin
from arbora.core.config_admin import (
    HostingDeploymentSettings,
    InvalidHostingDeploymentSettings,
    load_hosting_deployment_settings,
)


@pytest.mark.spec("ARBORA-DEPLOY-R009.1")
def test_does_not_import_agent_runtime_configuration_internals() -> None:
    """The module resolves hosting/deployment settings without reaching into
    the agent runtime's monolithic configuration module."""
    import_lines = [
        line.strip()
        for line in inspect.getsource(config_admin).splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert import_lines, "expected at least one import statement"
    assert not any("agent_utilities" in line for line in import_lines)


@pytest.mark.spec("ARBORA-DEPLOY-R009.1")
def test_resolves_defaults_from_empty_environment() -> None:
    settings = load_hosting_deployment_settings(env={})
    assert settings == HostingDeploymentSettings(
        host="0.0.0.0",
        port=8085,
        deploy_profile="dev",
        data_dir="/var/lib/arbora",
    )


@pytest.mark.spec("ARBORA-DEPLOY-R009.1")
def test_resolves_overrides_from_environment() -> None:
    settings = load_hosting_deployment_settings(
        env={
            "ARBORA_HOST": "127.0.0.1",
            "ARBORA_PORT": "9000",
            "ARBORA_DEPLOY_PROFILE": "prod",
            "ARBORA_DATA_DIR": "/data/arbora",
        }
    )
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.deploy_profile == "prod"
    assert settings.data_dir == "/data/arbora"


@pytest.mark.spec("ARBORA-DEPLOY-R009.1")
@pytest.mark.parametrize(
    "env",
    [
        {"ARBORA_PORT": "not-a-number"},
        {"ARBORA_PORT": "0"},
        {"ARBORA_PORT": "70000"},
        {"ARBORA_DEPLOY_PROFILE": "staging"},
    ],
)
def test_rejects_invalid_settings(env: dict[str, str]) -> None:
    with pytest.raises(InvalidHostingDeploymentSettings):
        load_hosting_deployment_settings(env=env)
