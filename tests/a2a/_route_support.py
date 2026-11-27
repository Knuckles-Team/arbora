"""The scripted engine route decision served tests share (ARBORA-A2A-R016.5)."""

from __future__ import annotations

from typing import Any

import pytest

__all__ = ["bind_route_decision", "bind_run_models"]


def bind_route_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the named-agent route question the way the served engine does.

    A message that names an agent is routed by the engine ``a2a_route`` decision
    (ARBORA-A2A-R016.5); the host's ``Decide`` port is unbound without an engine
    endpoint and refuses closed. The scripted engine below seals the one candidate the
    message names, so the served route reaches the run it streams.
    """
    from types import SimpleNamespace

    from arbora.mcp_server import host_context
    from tests.a2a.test_host_decision_served import HOST, SCHEMA, Engine

    build = host_context.build_host_context

    def build_bound() -> Any:
        host = build()
        host_context.bind_a2a_decision(
            host,
            SimpleNamespace(
                engine_send=Engine(),
                decision_feature_schema=lambda tenant: SCHEMA,
                decision_principal=lambda: HOST,
            ),
        )
        return host

    monkeypatch.setattr(host_context, "build_host_context", build_bound)


def bind_run_models(monkeypatch: pytest.MonkeyPatch, provider: Any) -> None:
    """Serve every run of the host built next on ``provider`` as its model port.

    The host binds its ``HostPorts`` once at composition; these served tests build the
    app without that step, so the model provider they control is bound on the host's
    run slot the moment the host context is built.
    """
    from meristem.host_ports import HostPorts

    from arbora.api.domain.agent_host.host_port_adapters import HostPortsBinding
    from arbora.mcp_server import host_context

    build = host_context.build_host_context

    def build_bound() -> Any:
        host = build()
        host.runs.ports.bind(HostPortsBinding(HostPorts(models=provider), None))
        return host

    monkeypatch.setattr(host_context, "build_host_context", build_bound)
