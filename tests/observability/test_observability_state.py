"""The host's observability field: owned state, separate per instance."""

from __future__ import annotations

from arbora.observability.state import Telemetry


def test_two_telemetry_objects_keep_separate_exporters_and_sequences():
    first, second = Telemetry(), Telemetry()
    first.install_exporter("exporter")
    assert first.exporter(lambda: "built") == "exporter"
    assert second.exporter(lambda: "built") == "built"
    first.reset_exporter()
    assert first.exporter(lambda: "rebuilt") == "rebuilt"
    a = first.next_event_sequence()
    assert first.next_event_sequence() > a
    assert not second.otel_initialized
    second.mark_otel_initialized(agent_instrumented=True)
    assert second.otel_initialized and second.agent_instrumented
    assert not first.otel_initialized


def test_two_health_states_keep_separate_windows_probes_and_authority():
    from arbora.observability.state import build_health_state

    first, second = build_health_state(), build_health_state()
    call = first.windows.begin("engine", 1.0)
    first.windows.end("engine", call, ok=False, elapsed=0.5)
    first.probes.bind("engine", lambda _session: {"status": "ok"})
    owner = first.authority.claim("session")
    assert second.windows.view(("engine",))["engine"] == ([], [])
    assert second.probes.get("engine") is None
    assert second.authority.session is None
    assert first.authority.release(owner)


def test_two_signal_buffers_keep_separate_rings_and_sequences():
    from arbora.observability.runtime_signals import record_runtime_signal
    from arbora.observability.state import GatewayTrend, RuntimeSignalBuffer

    first, second = RuntimeSignalBuffer(), RuntimeSignalBuffer()
    record_runtime_signal(first, "listener_restart", "telegram", {"delay_s": 1.0})
    assert len(first.snapshot()) == 1
    assert second.snapshot() == []
    assert first.next_sequence() == 0 and second.next_sequence() == 0
    assert len(first.drain()) == 1 and first.snapshot() == []
    one, other = GatewayTrend(), GatewayTrend()
    one.append_window({"avg": 1.0}, 50)
    assert other.history() == []
