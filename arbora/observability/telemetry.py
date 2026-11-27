"""The standard OTLP trace pipeline of the Arbora process.

The pipeline is opt-in. It starts only when a collector endpoint is set
(``MEDULLA_OBS_ADDR``, then ``OTEL_EXPORTER_OTLP_ENDPOINT``), the trace
exporter is not ``none``, and the OpenTelemetry SDK is installed. Every span is
exported through the metadata-only exporter, so no prompt or payload leaves the
process. The process bootstrap calls :func:`configure_standard_otel` once.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from arbora.core.environment import read_env
from arbora.observability.state import Telemetry

__all__ = ["configure_standard_otel", "otel_endpoint", "reset_standard_otel"]

logger = logging.getLogger(__name__)

#: The variables that name the collector, in precedence order.
_ENDPOINT_NAMES = ("MEDULLA_OBS_ADDR", "OTEL_EXPORTER_OTLP_ENDPOINT")


def otel_endpoint() -> str:
    """Return the configured collector endpoint, or ``""`` when none is set."""
    if str(read_env("OTEL_TRACES_EXPORTER") or "").strip().lower() == "none":
        return ""
    for name in _ENDPOINT_NAMES:
        value = str(read_env(name, "") or "").strip()
        if value:
            return value if "://" in value else f"http://{value}"
    return ""


def _build_provider(service_name: str, endpoint: str) -> Any:
    """Build and install the tracer provider, or ``None`` when the SDK is absent."""
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
        return None
    from arbora.observability.custom_observability import (
        _MetadataOnlySpanExporter,
        _service_topology_label,
        parse_otlp_headers,
    )

    service_ref = _service_topology_label(
        str(read_env("OTEL_SERVICE_NAME") or service_name)
    )
    headers = parse_otlp_headers(str(read_env("OTEL_EXPORTER_OTLP_HEADERS")))
    traces = endpoint.rstrip("/")
    if not traces.endswith("/v1/traces"):
        traces = f"{traces}/v1/traces"
    exporter = _MetadataOnlySpanExporter(
        OTLPSpanExporter(endpoint=traces, headers=headers),
        service_ref=service_ref,
    )
    provider = TracerProvider(resource=Resource.create({"service.name": service_ref}))
    # The exporter implements the SpanExporter protocol by duck typing.
    provider.add_span_processor(BatchSpanProcessor(cast(Any, exporter)))
    trace.set_tracer_provider(provider)
    return provider


def configure_standard_otel(
    telemetry: Telemetry, *, service_name: str = "arbora"
) -> bool:
    """Start the trace pipeline once and return whether it is active."""
    endpoint = otel_endpoint()
    if not endpoint:
        return False
    return telemetry.start_provider(lambda: _build_provider(service_name, endpoint))


def reset_standard_otel(telemetry: Telemetry) -> None:
    """Shut the pipeline down (tests and process shutdown)."""
    telemetry.stop_provider()
