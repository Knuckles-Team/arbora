"""Gateway self-telemetry → the shared health-intelligence kernels (ARBORA-HOST-R008).

CONCEPT:MERISTEM-OS.observability.unified-health-kernels (gateway producer) /
MERISTEM-KG.identity.evidence-spine-convergence (``MetricWindow`` locus).

Wires two previously-dead pieces off ONE real, already-computed signal — the
per-request latency the gateway's request middleware already measures for
its Prometheus histogram, then discards once it is handed to ``.observe()``:

1. ``arbora.observability.health``'s anomaly-detection kernel
   (:func:`~arbora.observability.health.compute_baseline` /
   :func:`~arbora.observability.health.detect_anomaly`) plus
   the metric-window evidence write
   had zero live callers anywhere in the agent runtime — a fully-built,
   generalized kernel with no producer wiring it. This module is that live
   caller.
2. ``MediaStore.store_metric_window_evidence`` (``eg_modality::EvidenceSpan::
   MetricWindow``) had no producer — an anomaly's own supporting trend window
   *is* the evidence for the claim "the gateway was anomalously slow here".

Bounded by design — NOT one evidence write per request. Request durations are
distilled into ONE trend row per ``_WINDOW_S`` window
(:class:`~arbora.observability.health.HealthTrendBuffer`, the same
distill-don't-dump cadence every other health-kernel consumer uses), and a
KG write only fires when ``detect_anomaly`` actually flags that window
against the gateway's own rolling baseline — a rare, meaningful event, never
a per-request write. Entirely best-effort: any failure here must never
affect the request/response path it's decoupled from (fire-and-forget via
``asyncio.create_task``).

Ported from the agent runtime's
``arbora.observability.gateway_health`` (moved wholesale, no
behavior change) per the ARBORA-HOST-R008 boundary move. The health
kernel and KG-ingest modules it calls (``arbora.observability.health``
/ ``health_ingest``) and the KG memory store
(``meristem.knowledge_graph.memory.native_ingest``) have not moved
yet; this module reaches back into the agent runtime for them, the normal
direction for an incremental migration (arbora depends on the agent
runtime, never the reverse).
"""

import asyncio
import json
import logging
import time
from typing import Any

from arbora.observability.state import GatewayTrend

logger = logging.getLogger(__name__)

# 5-minute distilled trend window — matches the health kernel's own
# fan-manager-derived default cadence intent (frequent enough to catch a
# real regression, coarse enough to keep the KG write rate bounded).
_WINDOW_S = 300
# Bounded rolling baseline history (~4h at the default window) — a module
# constant, not a flag: there is no per-deployment value that should differ.
_HISTORY_MAX = 50

_ENTITY_ID = "gateway"
_SIGNAL = "request_duration_seconds"


def _new_buffer() -> Any:
    from arbora.observability.health import HealthTrendBuffer

    return HealthTrendBuffer(window_s=_WINDOW_S)


def record_request_duration(trend_state: GatewayTrend, duration_s: float) -> None:
    """Feed one request's latency into the gateway's health-trend buffer.

    Called from the gateway's request middleware with the SAME ``duration``
    value it already computed for its own Prometheus histogram observation —
    no new instrumentation, just a second, cheap (in-memory, no I/O) consumer
    of a value that already exists. Only rarely (once per ``_WINDOW_S``
    window) does this produce a flushed trend; when it does, the
    anomaly-check + KG write is dispatched onto a background task so it
    never adds latency to the response the caller is still sending.
    """
    try:
        trend = trend_state.buffer(_new_buffer).add(float(duration_s), at=time.time())
    except Exception as e:  # must never affect the response path
        logger.debug("gateway health sample skipped: %s", e)
        return
    if trend is not None:
        try:
            # The state holds a strong reference until the check completes:
            # asyncio only holds a weak one, so an unreferenced task can be
            # collected mid-run.
            trend_state.hold(asyncio.create_task(_check_and_record(trend_state, trend)))
        except RuntimeError:
            # No running loop (e.g. a sync caller/test) — drop the check
            # rather than block; the next window's flush will try again.
            pass


def _media_store(trend_state: GatewayTrend) -> Any:
    store = trend_state.store()
    if store is None:
        raise RuntimeError("no metric evidence store is bound")
    return store


async def _check_and_record(trend_state: GatewayTrend, trend: dict[str, Any]) -> None:
    """Best-effort: baseline + anomaly-detect the just-flushed window against
    the gateway's rolling trend history; on a genuine anomaly, write a
    ``MetricWindow`` evidence locus carrying the exact triggering window.
    Never raises.
    """
    try:
        from arbora.observability import health as h

        history = trend_state.history()
        baseline = (
            h.compute_baseline(history, value_key="avg", peak_key="max")
            if len(history) >= h.DEFAULT_MIN_WINDOWS
            else None
        )
        anomaly = (
            h.detect_anomaly([trend], baseline, value_key="avg")
            if baseline is not None
            else None
        )
        trend_state.append_window(trend, _HISTORY_MAX)
        if anomaly is None:
            return

        store = _media_store(trend_state)
        data = json.dumps(trend, sort_keys=True).encode("utf-8")
        start_ms = int(float(trend.get("start_at") or 0.0) * 1000)
        end_ms = int(float(trend.get("end_at") or 0.0) * 1000)
        await store.store_metric_window_evidence(
            data,
            metric=f"{_ENTITY_ID}:{_SIGNAL}",
            start_ms=start_ms,
            end_ms=end_ms,
            mime_type="application/json",
            source="gateway-health",
        )
    except Exception as e:  # best-effort, never affects the request path
        logger.debug("gateway health anomaly check failed: %s", e)
