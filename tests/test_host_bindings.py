"""Startup bindings: the messaging ports, the send gate and the A2A mount path."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from graph_os import host_bindings as hb
from graph_os.messaging.host_ports import host_ports, reset_host_ports


@pytest.fixture(autouse=True)
def _unbind():
    reset_host_ports()
    yield
    reset_host_ports()


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


def test_bind_messaging_ports_binds_all_four_ports():
    hb.bind_messaging_ports()
    ports = host_ports()
    assert ports.turn_handler is hb.turn_handler
    assert ports.action_gate is hb.action_gate
    assert ports.outbound_mirror is hb.outbound_mirror
    assert ports.runtime_signal is not None
    ports.runtime_signal("listener_restart", "telegram", {"delay_s": 1.0})


def test_outbound_mirror_records_one_node_per_message():
    written = {}

    def add_node(node_id, label, properties):
        written[node_id] = (label, properties)

    message = SimpleNamespace(platform="slack", channel_id="c1", content="hi")
    engine = SimpleNamespace(add_node=add_node)
    asyncio.run(hb.outbound_mirror(message, engine))
    asyncio.run(hb.outbound_mirror(message, engine))
    assert len(written) == 1
    label, properties = next(iter(written.values()))
    assert label == "OutboundMessage"
    assert properties["text"] == "hi"


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
