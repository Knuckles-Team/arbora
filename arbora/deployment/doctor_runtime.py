"""Doctor checks for the runtime integrations and the engine connection (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import logging
from typing import Any

from arbora.deployment.doctor_support import PROBE_FAULTS, _result

logger = logging.getLogger(__name__)


def _inventory_format_ready(inventory_path: Any) -> bool:
    """Whether the inventory file parses as a bounded YAML mapping."""
    import yaml

    try:
        with inventory_path.open("rb") as stream:
            raw_inventory = stream.read(8 * 1024 * 1024 + 1)
        if len(raw_inventory) > 8 * 1024 * 1024:
            return False
        return isinstance(yaml.safe_load(raw_inventory.decode("utf-8")), dict)
    except (OSError, ValueError, yaml.YAMLError):  # readiness is redacted
        return False


def _inventory_readiness(raw_path: Any) -> tuple[bool, bool]:
    """``(file_ready, format_ready)`` for the optional infrastructure inventory."""
    from pathlib import Path

    try:
        inventory_path = Path(str(raw_path)).expanduser()
        file_ready = inventory_path.is_file()
    except (OSError, ValueError):
        return False, False
    if not file_ready:
        return False, False
    return True, _inventory_format_ready(inventory_path)


def _media_endpoint_count(cfg: Any) -> int:
    """How many of the nine optional media endpoints are configured."""
    return sum(
        bool(value)
        for value in (
            cfg.comfyui_url,
            cfg.xtts_url,
            cfg.openai_tts_url,
            cfg.whisper_url,
            cfg.faster_whisper_url,
            cfg.flux_url,
            cfg.sd35_url,
            cfg.hunyuan_url,
            cfg.svd_url,
        )
    )


def _check_runtime_integrations() -> dict[str, Any]:
    """Validate optional fleet, inventory, and media configuration offline.

    Endpoint identities and inventory paths are runtime-only material. This
    check reports aggregate configuration readiness and local file availability
    without returning any configured value or making a network request.
    """
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig

        cfg = AgentConfig()
        inventory_configured = bool(cfg.infra_inventory_path)
        inventory_file_ready = False
        inventory_format_ready = False
        if inventory_configured:
            inventory_file_ready, inventory_format_ready = _inventory_readiness(
                cfg.infra_inventory_path
            )
        fleet_template_configured = bool(cfg.fleet_mcp_url_template)
        media_endpoint_count = _media_endpoint_count(cfg)
    except PROBE_FAULTS as exc:  # doctor must remain defensive
        return _result(
            "runtime_integrations",
            "fail",
            f"runtime integration configuration is invalid ({type(exc).__name__})",
            remediation=(
                "Use bounded http(s) base URLs without inline credentials, a fleet "
                "template containing {server}, and a valid runtime inventory path."
            ),
            data={"ready": False, "redacted": True},
        )

    return _runtime_integrations_result(
        inventory_configured=inventory_configured,
        inventory_file_ready=inventory_file_ready,
        inventory_format_ready=inventory_format_ready,
        fleet_template_configured=fleet_template_configured,
        media_endpoint_count=media_endpoint_count,
    )


def _runtime_integrations_result(
    *,
    inventory_configured: bool,
    inventory_file_ready: bool,
    inventory_format_ready: bool,
    fleet_template_configured: bool,
    media_endpoint_count: int,
) -> dict[str, Any]:
    """Turn the resolved runtime-integration readiness flags into one verdict."""
    inventory_ready = inventory_file_ready and inventory_format_ready
    configured_category_count = sum(
        (
            inventory_configured,
            fleet_template_configured,
            media_endpoint_count > 0,
        )
    )
    ready_category_count = sum(
        (
            inventory_configured and inventory_ready,
            fleet_template_configured,
            media_endpoint_count > 0,
        )
    )
    data = {
        "configured_category_count": configured_category_count,
        "ready_category_count": ready_category_count,
        "inventory_configured": inventory_configured,
        "inventory_file_ready": inventory_file_ready,
        "inventory_format_ready": inventory_format_ready,
        "fleet_template_configured": fleet_template_configured,
        "media_endpoint_configured_count": media_endpoint_count,
        "media_endpoint_slot_count": 9,
        "network_probed": False,
        "redacted": True,
    }
    if not configured_category_count:
        return _result(
            "runtime_integrations",
            "skip",
            "optional inventory, fleet-template, and media endpoints are not configured",
            data=data,
        )
    if inventory_configured and not inventory_ready:
        return _result(
            "runtime_integrations",
            "warn",
            "runtime integration configuration is valid but the inventory file is "
            "unavailable or malformed",
            remediation=(
                "Point INFRA_INVENTORY_PATH at a readable, bounded YAML mapping, or "
                "unset it when infrastructure inventory ingestion is not used."
            ),
            data=data,
        )
    return _result(
        "runtime_integrations",
        "ok",
        f"{ready_category_count}/{configured_category_count} optional integration "
        "category(s) are configuration-ready",
        data=data,
    )


def _check_engine() -> dict[str, Any]:
    """Report the configured public EG coordinator contract, fully redacted.

    Runtime reachability is proven by Arbora composition itself. Doctor does
    not recreate AU engine election, placement, encryption, or autostart state.
    """
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig
        from arbora.deployment.production_ops import (
            ProductionOperationError,
            _transport_for_endpoint,
        )

        endpoints = [
            str(endpoint).strip()
            for endpoint in (AgentConfig().graph_service_endpoints or [])
            if str(endpoint).strip()
        ]
        if len(endpoints) != 1:
            raise ProductionOperationError(
                "Arbora requires exactly one configured EG coordinator"
            )
        transport = _transport_for_endpoint(endpoints[0])
    except PROBE_FAULTS as exc:  # doctor reports bounded metadata only
        return _result(
            "engine",
            "fail",
            f"engine coordinator configuration is invalid ({type(exc).__name__})",
            remediation=(
                "Configure exactly one tcp://, tls://, or unix:// "
                "GRAPH_SERVICE_ENDPOINTS coordinator."
            ),
            data={"configured_endpoint_count": 0, "redacted": True},
        )

    return _result(
        "engine",
        "ok",
        "engine coordinator transport is configured; live readiness is enforced at startup",
        data={
            "configured_endpoint_count": 1,
            "transport": "unix" if "socket_path" in transport else "network",
            "tls": bool(transport.get("tls", False)),
            "redacted": True,
        },
    )


def _check_engine_request_context() -> dict[str, Any]:
    """Report the fail-secure native-engine request-context posture."""

    data = {
        "verified_context_required": True,
        "legacy_protocol_available": False,
        "unauthenticated_transport_available": False,
    }
    return _result(
        "engine_request_context",
        "ok",
        "engine request context is current-only and requires verified identity",
        data=data,
    )
