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
