"""One trace record per ACP admission and capability request (ARBORA-ACP-R001.7).

The record names who asked for what and how it ended. It carries no file
content and no command output. The logger feeds the standard OpenTelemetry
exporter that ``arbora.observability.telemetry.configure_standard_otel`` sets
up; no second exporter exists here.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

ACP_LOGGER = "arbora.observability.acp"
_LOGGER = logging.getLogger(ACP_LOGGER)


@dataclass(frozen=True)
class AcpTraceRecord:
    session_id: str
    principal_id: str
    tenant: str
    workspace_root: str
    capability: str | None = None
    #: ``standing``, ``granted`` or ``denied``; ``None`` for an admission.
    permission_outcome: str | None = None
    refusal_reason: str | None = None


def emit_acp_record(record: AcpTraceRecord) -> None:
    """Emit one record on ``arbora.observability.acp``."""
    _LOGGER.info("acp.record", extra={"acp": asdict(record)})


__all__ = ["ACP_LOGGER", "AcpTraceRecord", "emit_acp_record"]
