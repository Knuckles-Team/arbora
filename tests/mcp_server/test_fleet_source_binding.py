"""The served runtime exposes the caller-bound FleetSourceExecutor factory."""

from __future__ import annotations

from typing import Any

from arbora.fleet.source_executor import FleetSourceExecutor
from arbora.mcp_server.served_invocation import _with_fleet_source_executor


def test_served_binding_builds_executor_from_graph_routed_engine(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_from_engine(
        cls: Any,
        gateway: Any,
        caller: Any,
        engine: Any,
        *,
        graph: str,
        overall_timeout_s: float,
    ) -> object:
        seen.update(
            gateway=gateway,
            caller=caller,
            engine=engine,
            graph=graph,
            overall_timeout_s=overall_timeout_s,
        )
        return object()

    monkeypatch.setattr(
        FleetSourceExecutor,
        "from_engine",
        classmethod(fake_from_engine),
    )
    gateway = object()
    bindings = _with_fleet_source_executor(
        {"fleet_gateway": gateway}, object(), "tenant:graph"
    )

    assert bindings is not None
    factory = bindings["fleet_source_executor"]
    caller = object()
    assert factory(caller, overall_timeout_s=2.5) is not None
    assert seen["gateway"] is gateway
    assert seen["caller"] is caller
    assert seen["graph"] == "tenant:graph"
    assert seen["overall_timeout_s"] == 2.5
    assert seen["engine"].graph_compute.for_graph("tenant:graph").graph_name == (
        "tenant:graph"
    )


def test_served_binding_does_not_replace_explicit_reader() -> None:
    existing = object()
    bindings = {"fleet_gateway": object(), "fleet_source_executor": existing}
    assert _with_fleet_source_executor(bindings, object(), "g") == bindings
