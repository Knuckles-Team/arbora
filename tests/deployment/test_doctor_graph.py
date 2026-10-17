"""arbora doctor: engine served-tool-family and graph-connection checks.

Moved from agent-utilities (tests/unit/deployment/test_doctor.py BUG-013 rows
and tests/unit/deployment/test_doctor_graph_connections.py) when the harness
was cut; arbora serves the engine families and owns the connection registry.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from arbora.deployment import doctor as D
from arbora.deployment import doctor as doctor_module


def test_bug013_engine_domains_doctor_fails_closed_on_production_profile(monkeypatch):
    """BUG-013: a required capability module (the `epistemic_graph` client,
    which populates the 21 REQUIRED `engine_<domain>` tool families) silently
    absent must FAIL readiness on a real deployment profile, not warn/skip
    past it. Reproduces the exact live shape: `engine_tools.ENGINE_DOMAINS`
    degrades to `{}` (its own documented graceful-but-loud fallback) when the
    `epistemic_graph` package is not importable."""
    from arbora.mcp_server.backing.tools import engine_tools

    cfg = SimpleNamespace(deployment_profile="single-node-prod")
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)
    monkeypatch.setattr(engine_tools, "ENGINE_DOMAINS", {})

    result = D._check_engine_domains()

    assert result["status"] == "fail"
    assert result["data"]["domain_count"] == 0
    assert result["data"]["profile"] == "single-node-prod"
    assert result["remediation"]


def test_bug013_engine_domains_doctor_warns_not_fails_on_tiny_profile(monkeypatch):
    """`tiny` is the one documented engine-less pre-bootstrap profile — the
    same distinction `_check_config` already draws — so the same absence is a
    `warn`, not a `fail`, there."""
    from arbora.mcp_server.backing.tools import engine_tools

    cfg = SimpleNamespace(deployment_profile="tiny")
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)
    monkeypatch.setattr(engine_tools, "ENGINE_DOMAINS", {})

    result = D._check_engine_domains()

    assert result["status"] == "warn"


def test_bug013_engine_domains_doctor_ok_when_domains_registered(monkeypatch):
    """Negative-direction proof: a real, populated registry reports `ok` on
    every profile — this check does not fail a healthy deployment."""
    from arbora.mcp_server.backing.tools import engine_tools

    cfg = SimpleNamespace(deployment_profile="enterprise")
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)
    monkeypatch.setattr(
        engine_tools, "ENGINE_DOMAINS", {"nodes": ["get_node"], "edges": ["get_edge"]}
    )

    result = D._check_engine_domains()

    assert result["status"] == "ok"
    assert result["data"]["domain_count"] == 2


def test_bug013_engine_domains_wired_into_run_doctor_and_fails_the_sweep(monkeypatch):
    """Wiring proof through the REAL aggregator entrypoint (`run_doctor`), not
    just the standalone check function — proves `engine_domains` is actually
    registered in `CHECKS` and that its `fail` status propagates to the
    overall sweep verdict, exactly like every other doctor check."""
    from arbora.mcp_server.backing.tools import engine_tools

    cfg = SimpleNamespace(deployment_profile="single-node-prod")
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)
    monkeypatch.setattr("arbora.core.settings.load_config", lambda: None)
    monkeypatch.setattr(engine_tools, "ENGINE_DOMAINS", {})

    report = D.run_doctor(only=["engine_domains"])

    assert report["status"] == "unhealthy"
    names = [c["name"] for c in report["checks"]]
    assert names == ["engine_domains"]
    assert report["checks"][0]["status"] == "fail"


class _Registry:
    def __init__(self, *, probe_error: bool = False) -> None:
        self.probe_error = probe_error
        self.probed: list[str] = []

    def status(self) -> dict[str, object]:
        return {
            "connections": [
                {"name": "default", "role": "authority"},
                {
                    "name": "external-source",
                    "role": "read",
                    "backend_type": "graphql",
                },
            ]
        }

    def probe(self, name: str) -> bool:
        self.probed.append(name)
        if self.probe_error:
            raise ValueError(
                "secret://source/auth https://private.example.test/graphql"
            )
        return True


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        external_graph_connectors=[],
        kg_connections=[
            {
                "name": "external-source",
                "source_alias": "external-source",
                "backend": "graphql",
                "role": "read",
                "connection_profile_ref": "secret://source/connection",
                "mapping_policy_ref": "secret://source/mapping",
                "auth_profile_ref": "secret://source/auth",
                "tls_profile_ref": "secret://source/tls",
            }
        ],
    )


def test_doctor_probes_kg_connections_only_and_returns_aggregate_metadata(
    monkeypatch,
) -> None:
    registry = _Registry()
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", _config)
    monkeypatch.setattr(
        "arbora.mcp_server.backing.engine_routing.get_connection_registry",
        lambda: registry,
    )

    result = doctor_module._check_graph_connections(live=True)
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "ok"
    assert registry.probed == ["external-source"]
    assert result["data"] == {
        "configured_count": 1,
        "registered_count": 1,
        "ready_count": 1,
        "probe_failed_count": 0,
        "invalid_declaration_count": 0,
        "duplicate_declaration_count": 0,
        "missing_declaration_count": 0,
        "stalled_mirror_count": 0,
        "roles": {"read": 1},
        "redacted": True,
        "live_probed": True,
    }
    assert "external-source" not in rendered
    assert "secret://" not in rendered
    assert "private.example.test" not in rendered


def test_doctor_fails_closed_without_leaking_connection_probe_errors(
    monkeypatch,
) -> None:
    registry = _Registry(probe_error=True)
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", _config)
    monkeypatch.setattr(
        "arbora.mcp_server.backing.engine_routing.get_connection_registry",
        lambda: registry,
    )

    result = doctor_module._check_graph_connections(live=True)
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "fail"
    assert result["data"]["probe_failed_count"] == 1
    assert result["data"]["ready_count"] == 0
    assert "external-source" not in rendered
    assert "secret://" not in rendered
    assert "private.example.test" not in rendered


def test_doctor_static_check_never_opens_connection_transport(monkeypatch) -> None:
    registry = _Registry(probe_error=True)
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", _config)
    monkeypatch.setattr(
        "arbora.mcp_server.backing.engine_routing.get_connection_registry",
        lambda: registry,
    )

    result = doctor_module._check_graph_connections(live=False)

    assert result["status"] == "ok"
    assert result["data"]["live_probed"] is False
    assert result["data"]["ready_count"] == 0
    assert registry.probed == []


pytestmark = pytest.mark.spec("GRAPHOS-DEPLOY-R007.6")
