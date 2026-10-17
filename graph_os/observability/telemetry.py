"""The standard OTLP trace pipeline of the Graph OS process.

The pipeline is opt-in. It starts only when a collector endpoint is set
(``EPISTEMIC_GRAPH_OBS_ADDR``, then ``OTEL_EXPORTER_OTLP_ENDPOINT``), the trace
exporter is not ``none``, and the OpenTelemetry SDK is installed. Every span is
exported through the metadata-only exporter, so no prompt or payload leaves the
process. The process bootstrap calls :func:`configure_standard_otel` once.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from graph_os.core.settings import setting

__all__ = ["configure_standard_otel", "otel_endpoint", "reset_standard_otel"]

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_provider: Any = None


def otel_endpoint() -> str:
    """Return the configured collector endpoint, or ``""`` when none is set."""
    if str(setting("OTEL_TRACES_EXPORTER", "") or "").strip().lower() == "none":
        return ""
    for name in ("EPISTEMIC_GRAPH_OBS_ADDR", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        value = str(setting(name, "") or "").strip()
        if value:
            return value if "://" in value else f"http://{value}"
    return ""


def configure_standard_otel(*, service_name: str = "graph-os") -> bool:
    """Start the trace pipeline once and return whether it is active."""
    global _provider
    endpoint = otel_endpoint()
    if not endpoint:
        return False
    with _lock:
        if _provider is not None:
            return True
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            logger.warning("OTLP export is disabled: the OpenTelemetry SDK is absent")
            return False
        from graph_os.observability.custom_observability import (
            _MetadataOnlySpanExporter,
            _service_topology_label,
            parse_otlp_headers,
        )

        service_ref = _service_topology_label(
            str(setting("OTEL_SERVICE_NAME", "") or service_name)
        )
        headers = parse_otlp_headers(str(setting("OTEL_EXPORTER_OTLP_HEADERS", "")))
        traces = endpoint.rstrip("/")
        if not traces.endswith("/v1/traces"):
            traces = f"{traces}/v1/traces"
        exporter = _MetadataOnlySpanExporter(
            OTLPSpanExporter(endpoint=traces, headers=headers),
            service_ref=service_ref,
        )
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_ref})
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _provider = provider
        return True


def reset_standard_otel() -> None:
    """Shut the pipeline down (tests and process shutdown)."""
    global _provider
    with _lock:
        if _provider is not None:
            _provider.shutdown()
        _provider = None
