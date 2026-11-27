"""Shared signed inbound webhook ingress (ARBORA-MESSAGING-R008.1).

``POST /api/v1/messaging/inbound/{platform}`` needs no bearer: the adapter's
own signature check (``MessagingBackend.verify_inbound``) is the only
admission. The verified event goes on that backend's queue, which ``listen``
drains; the sender allowlist of ``ARBORA-MESSAGING-R007`` still applies after
that. One log line per refusal names the platform and the code, never the body.
"""

from __future__ import annotations

import enum
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from arbora.core.environment import read_env
from arbora.messaging.base import InboundSignatureInvalid

logger = logging.getLogger(__name__)

MAX_BYTES_DEFAULT = 1_048_576
MAX_BYTES_RANGE = (1024, 10_485_760)


class InboundRefusal(enum.Enum):
    """Refusal code and its HTTP status."""

    INBOUND_SIGNATURE_INVALID = 401
    INBOUND_PLATFORM_UNKNOWN = 404
    INBOUND_TOO_LARGE = 413
    INBOUND_INTAKE_DISABLED = 503


def intake_enabled() -> bool:
    """True only when intake is explicitly on and this is the host process."""
    flag = str(read_env("MESSAGING_INTAKE_ENABLED", "") or "").strip().lower()
    role = str(read_env("KG_DAEMON_ROLE", "client") or "").strip()
    return flag in {"1", "true", "yes", "on"} and role == "host"


def max_bytes() -> int:
    """The body limit from ``MESSAGING_INBOUND_MAX_BYTES``, clamped to its range."""
    try:
        value = int(read_env("MESSAGING_INBOUND_MAX_BYTES", MAX_BYTES_DEFAULT))
    except (TypeError, ValueError):
        value = MAX_BYTES_DEFAULT
    return max(MAX_BYTES_RANGE[0], min(MAX_BYTES_RANGE[1], value))


def _refuse(platform: str, refusal: InboundRefusal) -> JSONResponse:
    logger.warning("inbound refused platform=%s code=%s", platform, refusal.name)
    return JSONResponse({"error": refusal.name}, status_code=refusal.value)


async def handle_inbound(request: Request, *, service: Any = None) -> JSONResponse:
    """Verify one signed event and queue it on the running backend."""
    platform = str(request.path_params.get("platform", ""))
    if not intake_enabled():
        return _refuse(platform, InboundRefusal.INBOUND_INTAKE_DISABLED)
    if service is None:
        from arbora.messaging.service import MessagingService

        service = MessagingService.instance()
    backend = await service.get_backend(platform)
    if backend is None:
        return _refuse(platform, InboundRefusal.INBOUND_PLATFORM_UNKNOWN)
    limit = max_bytes()
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return _refuse(platform, InboundRefusal.INBOUND_TOO_LARGE)
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return _refuse(platform, InboundRefusal.INBOUND_TOO_LARGE)
    try:
        event = backend.verify_inbound(dict(request.headers), body)
        await backend.enqueue_inbound(event)
    except InboundSignatureInvalid:
        return _refuse(platform, InboundRefusal.INBOUND_SIGNATURE_INVALID)
    return JSONResponse({"accepted": True}, status_code=202)


def register_inbound_route(app: Any, prefix: str = "/api") -> None:
    """Register the shared inbound route beside the ACP routes."""
    app.add_route(
        f"{prefix}/v1/messaging/inbound/{{platform}}", handle_inbound, methods=["POST"]
    )
