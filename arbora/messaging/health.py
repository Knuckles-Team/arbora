"""Configuration-aware messaging intake health (ARBORA-MESSAGING-R005.1).

``messaging_intake_component`` builds ``components.messaging_intake`` for the
authenticated health reads. It reads the registry only: no backend is created,
no message is sent and no credential value is returned. Rows carry a closed
reason code, never raw exception text.
"""

from __future__ import annotations

from typing import Any

from arbora.messaging.supervision import AdapterImportFailed, AdapterMissing
from arbora.messaging.webhook_ingress import intake_enabled

__all__ = ["messaging_intake_component"]

ADAPTER_MISSING = "adapter_missing"
ADAPTER_IMPORT_FAILED = "adapter_import_failed"
BACKEND_NOT_CONNECTED = "backend_not_connected"


def _reason(registry: Any, backend_id: str, connected: bool) -> str | None:
    typed = registry.degraded_reason(backend_id)
    if isinstance(typed, AdapterMissing):
        return ADAPTER_MISSING
    if isinstance(typed, AdapterImportFailed):
        return ADAPTER_IMPORT_FAILED
    return None if connected else BACKEND_NOT_CONNECTED


def messaging_intake_component(
    registry: Any = None, *, enabled: bool | None = None
) -> dict[str, Any]:
    """Return ``{enabled, configured, state, alert_eligible, backends}``."""
    if registry is None:
        from arbora.messaging.registry import MessagingRegistry

        registry = MessagingRegistry.instance()
    on = intake_enabled() if enabled is None else enabled
    configured_ids = set(registry.configured_backend_ids())
    rows: list[dict[str, Any]] = []
    for backend_id in sorted(set(registry.list_backends()) | configured_ids):
        configured = backend_id in configured_ids
        if not configured:
            rows.append(
                {
                    "id": backend_id,
                    "configured": False,
                    "state": "not_configured",
                    "reason_code": None,
                }
            )
            continue
        if not on:
            rows.append(
                {
                    "id": backend_id,
                    "configured": True,
                    "state": "disabled",
                    "reason_code": None,
                }
            )
            continue
        connected = bool(
            getattr(registry.get_backend(backend_id), "is_connected", False)
        )
        rows.append(
            {
                "id": backend_id,
                "configured": True,
                "state": "connected" if connected else "unavailable",
                "reason_code": _reason(registry, backend_id, connected),
            }
        )
    configured_any = bool(configured_ids)
    if not on:
        state = "disabled"
    elif not configured_any:
        state = "not_configured"
    elif any(row["state"] == "unavailable" for row in rows):
        state = "degraded"
    else:
        state = "healthy"
    return {
        "enabled": on,
        "configured": configured_any,
        "state": state,
        "alert_eligible": on and state in {"not_configured", "degraded"},
        "backends": rows,
    }
