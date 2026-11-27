"""One ``HostPorts`` record replaces the harness register calls (ARBORA-HOST-R040.2)."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from meristem.host_ports import HostPorts

from arbora.api.domain.agent_host.host_port_adapters import (
    HostPortsBinding,
    HostPortsSlot,
)
from arbora.mcp_server import harness_ports
from arbora.mcp_server.host_context import build_host_context
from arbora.pin_lag.meristem import PinLagUnavailable

ROOT = Path(__file__).resolve().parents[2]
REMOVED = {
    "bind_run_ports",
    "register_harness_host",
    "unregister_harness_host",
    "register_run_decision_provider",
    "register_model_provider",
    "register_run_event_sink",
    "register_tool_caller",
    "register_native_toolset_provider",
    "register_fleet_catalog_provider",
    "register_remote_toolset_provider",
    "register_tool_client_factory",
    "register_authority_keepalive",
    "register_run_history_provider",
    "register_run_checkpoint_factory",
    "register_optional_scaffold_publication_factory",
}


def _slot(**fields: Any) -> HostPortsSlot:
    slot = HostPortsSlot()
    slot.bind(HostPortsBinding(HostPorts(**fields), None))
    return slot


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    return None


@pytest.mark.spec("ARBORA-HOST-R040.2")
def test_no_register_call_remains() -> None:
    offenders: list[str] = []
    for path in sorted((ROOT / "arbora").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders.extend(
            f"{path.relative_to(ROOT)}:{node.lineno}"
            for node in ast.walk(tree)
            if _name_of(node) in REMOVED
        )
    assert offenders == []


@pytest.mark.spec("ARBORA-HOST-R040.2")
def test_build_host_ports_sets_every_named_provider_on_the_real_record() -> None:
    host = build_host_context()
    ports = harness_ports.build_host_ports(host)
    from arbora.agent_runs import decide_run, record_run_event, resolve_model

    assert type(ports) is HostPorts
    assert ports.decide is decide_run
    assert ports.models is resolve_model
    assert ports.events.func is record_run_event
    assert ports.events.keywords == {"engine_provider": host.engine.served}
    for name in (
        "native",
        "remote",
        "tool_client",
        "keepalive",
        "scaffold_publication",
        "checkpoint_store",
    ):
        assert getattr(ports, name) is not None, name
    assert ports.fleet is None  # no multiplexer is served


@pytest.mark.spec("ARBORA-HOST-R040.2")
def test_a_structural_double_of_the_record_receives_the_same_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclasses.dataclass(frozen=True)
    class Double:
        decide: Any = None
        models: Any = None
        events: Any = None
        native: Any = None
        fleet: Any = None
        remote: Any = None
        tool_client: Any = None
        keepalive: Any = None
        scaffold_publication: Any = None
        checkpoint_store: Any = None
        probe: Any = None

    from arbora.pin_lag import meristem as lag

    monkeypatch.setattr(lag, "host_ports_type", lambda: Double)
    monkeypatch.setattr(
        lag, "_host_ports_module", lambda: SimpleNamespace(HostPorts=Double)
    )
    ports = harness_ports.build_host_ports(build_host_context())
    assert isinstance(ports, Double)
    assert callable(ports.decide)
    assert ports.native is not None


@pytest.mark.spec("ARBORA-HOST-R040.2")
def test_a_record_without_a_field_refuses_naming_it_and_the_slot_holds_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclasses.dataclass(frozen=True)
    class Short:
        decide: Any = None

    from arbora.pin_lag import meristem as lag

    monkeypatch.setattr(
        lag, "_host_ports_module", lambda: SimpleNamespace(HostPorts=Short)
    )
    host = build_host_context()
    with pytest.raises(PinLagUnavailable) as raised:
        harness_ports.build_host_ports(host)
    assert raised.value.missing == "meristem.host_ports.HostPorts.models"
    harness_ports.bind_host_ports_of(host)
    with pytest.raises(PinLagUnavailable):
        host.runs.ports.current()


@pytest.mark.spec("ARBORA-HOST-R040.2")
def test_an_unbound_slot_is_the_empty_record_and_two_hosts_share_no_slot() -> None:
    first, second = build_host_context(), build_host_context()
    assert first.runs.ports is not second.runs.ports
    assert first.runs.ports.current() == HostPorts()
    harness_ports.bind_host_ports_of(first)
    assert first.runs.ports.current().decide is not None
    assert second.runs.ports.current() == HostPorts()


@pytest.mark.spec("ARBORA-HOST-R040.2")
def test_every_run_entry_point_receives_the_host_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = HostPorts(events=lambda _event: None)
    slot = _slot(events=marker.events)
    seen: dict[str, Any] = {}

    import meristem.graph as graph
    import meristem.rlm as rlm

    class Executor:
        def __init__(self, config: Any = None, *, ports: Any = None) -> None:
            seen["executor"] = ports

    async def fake_graph(*args: Any, ports: Any = None, **kw: Any) -> dict:
        seen["graph"] = ports
        return {}

    async def fake_rlm(*args: Any, ports: Any = None, **kw: Any) -> dict:
        seen["rlm"] = ports
        return {"ok": True, "result": "x", "usage": {}}

    monkeypatch.setattr(graph, "AgentRunExecutor", Executor)
    monkeypatch.setattr(graph, "execute_graph", fake_graph)
    monkeypatch.setattr(rlm, "run_rlm", fake_rlm)

    from arbora.agent_host import _default_executor
    from arbora.agent_runs import AgentRunPort
    from arbora.api.contracts.agent_control_plane import AgentControlPlane
    from arbora.run_origin import RunOriginRegistry

    _ = AgentRunPort(origins=RunOriginRegistry(), ports=slot)._executor
    assert seen["executor"] == slot.current()

    _default_executor(slot.current())(object(), {}, "q")
    assert seen["graph"] == slot.current()

    plane = object.__new__(AgentControlPlane)
    plane._ports = slot
    request = SimpleNamespace(task="t", input_text="i")
    asyncio.run(AgentControlPlane._execute(plane, request))
    assert seen["rlm"] == slot.current()

    from arbora.api.ops.plans import _run_request

    plan = {"output_task_id": "t1", "tasks": []}
    assert _run_request(plan, "run-1", slot.current()).ports == slot.current()

    from arbora.acp.local_history import LocalConversationHistory

    history = LocalConversationHistory("model:test")
    history.bind_ports(slot.current())
    from arbora.acp.session_policy import PrincipalContext

    request = asyncio.run(
        history.request(PrincipalContext("a", "t", frozenset()), "s1", "hello")
    )
    assert request.ports == slot.current()
