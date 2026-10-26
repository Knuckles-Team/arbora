"""Wiring checks for the deployment extraction's direct console scripts."""

from __future__ import annotations

import tomllib
from pathlib import Path

from arbora.deployment import release_canary


def _project_scripts() -> dict[str, str]:
    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]["scripts"]


def test_host_deployment_scripts_point_to_arbora_modules() -> None:
    scripts = _project_scripts()
    assert scripts == {
        "agent-utilities-doctor": "arbora.deployment.doctor:main",
        "agent-utilities-venv": "arbora.deployment.venv_sync:main",
        "arbora": "arbora.console:main",
        "arbora-daemon": "arbora.gateway.daemon:main",
        "arbora-acp": "arbora.acp.local_runtime:main",
        "arbora-production-ops": "arbora.deployment.production_ops:main",
        "arbora-connector-count-pins": "arbora.fleet.connector_count_pins:main",
        "arbora-release-canary": "arbora.deployment.release_canary:main",
        "arbora-capability-layers": "arbora.architecture.capability_layers:main",
        "arbora-check-import-boundaries": "arbora.architecture.boundary_census:main",
        "arbora-hosting-config": "arbora.deployment.host_config:main",
        "arbora-wiring-sweep": "arbora.evolution.wiring_sweep:main",
        "setup-config": "arbora.deployment.cli:main",
    }


def test_release_canary_checks_the_same_direct_entrypoints() -> None:
    assert release_canary._ENTRY_POINTS == {
        "arbora": "arbora.console:main",
        "agent-utilities-doctor": "arbora.deployment.doctor:main",
    }


def test_arbora_entrypoint_resolves_to_the_native_server() -> None:
    from arbora.mcp_server.server import mcp_server

    assert callable(mcp_server)
