"""The engine-backed store for anomalous gateway metric windows (ARBORA-HOST-R039.4).

The gateway's latency trend keeps an anomalous window as evidence. This store writes
that window to the engine as one content-addressed media blob under the process
session's graph. The host composition binds it into ``GatewayTrend``; nothing
reaches it through a module name.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

__all__ = ["EngineMetricEvidence"]


class EngineMetricEvidence:
    """Stores one metric window as engine media, keyed by the window's own digest."""

    def __init__(
        self, engine_provider: Callable[[], Any], process_session: Callable[[], Any]
    ) -> None:
        self._engine_provider = engine_provider
        self._process_session = process_session

    async def store_metric_window_evidence(
        self,
        data: bytes,
        *,
        metric: str,
        start_ms: int,
        end_ms: int,
        mime_type: str,
        source: str,
    ) -> str:
        """Write the window and return its engine digest; refuse without a session."""
        from arbora.pin_lag.meristem import media_methods

        media_methods()
        from medulla.generated import storage

        session = self._process_session()
        if session is None or not session.graph:
            raise RuntimeError("no process session to store metric evidence under")
        envelope = json.dumps(
            {
                "metric": metric,
                "source": source,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "mime_type": mime_type,
                "window": data.decode("utf-8"),
            },
            sort_keys=True,
        ).encode("utf-8")
        graph = str(session.graph)
        client = self._engine_provider().graph_compute.for_graph(graph).async_client
        return await storage.send_media_put(
            client,
            {"media_type": "application/json", "data": envelope},
            graph,
            idempotency_key=f"metric-window:{hashlib.sha256(envelope).hexdigest()}",
        )
