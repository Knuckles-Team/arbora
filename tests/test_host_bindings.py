"""Startup bindings: the messaging ports, the send gate and the A2A mount path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arbora import host_bindings as hb
from arbora.core.agent_names import FLEET_TOOLS
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.host_caches import ToolTable
from arbora.messaging.host_ports import host_ports, reset_host_ports
from arbora.observability.state import RuntimeSignalBuffer
from arbora.security.process_authority import ProcessAuthority


@pytest.fixture(autouse=True)
def _unbind():
    reset_host_ports()
    yield
    reset_host_ports()


@pytest.mark.spec("ARBORA-HOST-R008.1001")
@pytest.mark.parametrize(
    ("session", "allowed"),
    [
        (None, False),
        (SimpleNamespace(scopes=frozenset({"kg:read"})), False),
        (SimpleNamespace(scopes=frozenset({"kg:write"})), True),
        (SimpleNamespace(scopes=frozenset({"kg:admin"})), True),
    ],
)
def test_send_gate_admits_only_a_session_with_a_send_scope(
    monkeypatch, session, allowed
):
    monkeypatch.setattr(hb, "_current_session", lambda: session)
    decision = hb.action_gate({"kind": "message.send"}, engine=None)
    assert decision.allowed is allowed
    assert decision.decision == ("allow" if allowed else "deny")


@pytest.mark.spec("ARBORA-HOST-R008.1001")
def test_decision_is_a_frozen_record_and_the_fleet_toolset_has_two_tools():
    import dataclasses

    assert FLEET_TOOLS == ("find", "act")
    assert dataclasses.is_dataclass(hb.SendDecision)
    fields = {f.name for f in dataclasses.fields(hb.SendDecision)}
    assert {"allowed", "decision", "reason"} == fields
    decision = hb.action_gate({"kind": "message.send"}, engine=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.allowed = True  # type: ignore[misc]


@pytest.mark.spec("ARBORA-HOST-R008.1000")
@pytest.mark.spec("ARBORA-HOST-R008.1003")
def test_bind_messaging_ports_binds_all_four_ports():
    tools = ToolTable(ProcessAuthority(config=RuntimeConfig()))
    hb.bind_messaging_ports(lambda: None, tools, RuntimeSignalBuffer(), {})
    ports = host_ports()
    assert ports.turn_handler.func is hb.turn_handler
    assert ports.turn_handler.args == (tools,)
    assert ports.action_gate is hb.action_gate
    assert ports.outbound_mirror is hb.outbound_mirror
    assert ports.runtime_signal is not None
    ports.runtime_signal("listener_restart", "telegram", {"delay_s": 1.0})


@pytest.mark.spec("ARBORA-HOST-R008.1002")
def test_outbound_mirror_records_one_node_per_message():
    written = {}

    def create_node(node_id, label, properties):
        if node_id in written:
            return False
        written[node_id] = (label, properties)
        return True

    message = SimpleNamespace(platform="slack", channel_id="c1", content="hi")
    engine = SimpleNamespace(create_node=create_node)
    asyncio.run(hb.outbound_mirror(message, engine))
    asyncio.run(hb.outbound_mirror(message, engine))
    assert len(written) == 1
    label, properties = next(iter(written.values()))
    assert label == "OutboundMessage"
    assert properties["text"] == "hi"


@pytest.mark.spec("ARBORA-HOST-R008.1005")
@pytest.mark.parametrize(
    ("path", "inner"),
    [
        ("/a2a/agent", "/"),
        ("/a2a/agent/.well-known/agent-card.json", "/.well-known/agent-card.json"),
    ],
)
def test_prefixed_app_strips_the_mount_path(path, inner):
    seen = {}

    async def app(scope, receive, send):
        seen["path"] = scope["path"]

    asyncio.run(
        hb._PrefixedApp(app, hb.A2A_PATH)({"type": "http", "path": path}, None, None)
    )
    assert seen["path"] == inner
