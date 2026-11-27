"""The host's observability field: owned state, separate per instance."""

from __future__ import annotations

import pytest

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


def test_two_metrics_objects_keep_separate_samplers():
    from arbora.observability.state import Metrics

    first, second = Metrics(), Metrics()

    def sampler() -> None:
        return None

    first.register(sampler)
    first.register(sampler)
    assert first.samplers() == (sampler,)
    assert second.samplers() == ()


def test_a_scrape_runs_the_samplers_of_the_metrics_it_is_given():
    from arbora.observability.gateway_metrics import (
        PROMETHEUS_AVAILABLE,
        render_metrics,
    )
    from arbora.observability.state import Metrics

    if not PROMETHEUS_AVAILABLE:
        return
    ran: list[str] = []
    first, second = Metrics(), Metrics()
    first.register(lambda: ran.append("first"))
    second.register(lambda: ran.append("second"))
    render_metrics(first)
    assert ran == ["first"]


def test_host_composition_samples_fleet_gauges_and_binds_the_evidence_store():
    from arbora.mcp_server.host_context import build_host_context
    from arbora.observability.metric_evidence import EngineMetricEvidence

    host = build_host_context()
    other = build_host_context()
    assert host.fleet.multiplexers.sample_child_health_gauges in (
        host.observability.metrics.samplers()
    )
    assert other.observability.metrics.samplers() != (
        host.observability.metrics.samplers()
    )
    store = host.observability.signals.gateway.store()
    assert isinstance(store, EngineMetricEvidence)
    assert other.observability.signals.gateway.store() is not store


@pytest.mark.asyncio
async def test_the_evidence_store_writes_the_window_under_the_process_session(
    monkeypatch,
):
    from types import SimpleNamespace

    from medulla.generated import storage

    from arbora.observability.metric_evidence import EngineMetricEvidence

    sent: list[tuple[object, dict, str, str]] = []

    async def send_media_put(client, params, graph, *, idempotency_key):
        sent.append((client, params, graph, idempotency_key))
        return "sha256:abc"

    monkeypatch.setattr(storage, "send_media_put", send_media_put, raising=False)
    monkeypatch.setattr("arbora.pin_lag.meristem.media_methods", lambda: ())
    client = object()
    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(async_client=client)
        )
    )
    store = EngineMetricEvidence(lambda: engine, lambda: SimpleNamespace(graph="g1"))
    digest = await store.store_metric_window_evidence(
        b'{"avg": 5.0}',
        metric="gateway:latency",
        start_ms=1,
        end_ms=2,
        mime_type="application/json",
        source="gateway-health",
    )
    assert digest == "sha256:abc"
    assert sent[0][0] is client and sent[0][2] == "g1"
    assert sent[0][3].startswith("metric-window:")
    unbound = EngineMetricEvidence(lambda: engine, lambda: None)
    with pytest.raises(RuntimeError):
        await unbound.store_metric_window_evidence(
            b"{}",
            metric="m",
            start_ms=0,
            end_ms=0,
            mime_type="application/json",
            source="s",
        )
