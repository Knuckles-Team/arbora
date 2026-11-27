"""``POST /development/scm-events/{connector}``: signed code-host deliveries (ARBORA-OPS-R896.8).

The gateway identity middleware admits a delivery by its signature, replay id and skew and
serves it as the source's service principal (``arbora.security.signed_ingress``). This route
trusts only the marker that middleware sets on the request scope. It never reads a bearer and
never admits a request itself: with no marker it answers the uniform 401. The admitted
delivery runs through the one invoke chokepoint as ``development.scm.event``.
"""

from __future__ import annotations

from collections.abc import Callable
from secrets import token_hex
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from arbora.security.signed_ingress import (
    ADMITTED_STATE_KEY,
    SIGNED_CREDENTIAL_KIND,
    AdmittedIngress,
)

from .routes import _error, caller_session_scope

SCM_EVENT_OP = "development.scm.event"


def mount_scm_events_route(
    app: FastAPI,
    *,
    services: Any,
    invoke: Any,
    response: Callable[[Any, Any, str], JSONResponse],
) -> None:
    """Add the signed-ingress route to ``app``; ``invoke`` is the one chokepoint."""

    async def scm_event(request: Request, connector: str) -> JSONResponse:
        from arbora.api.mcp.caller import caller_from_session
        from arbora.api.ops.development_scm import body_params
        from arbora.api.registry import Surface
        from arbora.security.session import current_session

        digest = services.registry.digest
        admitted = (request.scope.get("state") or {}).get(ADMITTED_STATE_KEY)
        session = current_session()
        if not isinstance(admitted, AdmittedIngress) or session is None:
            return _error("UNAUTHENTICATED", digest=digest, op=SCM_EVENT_OP)
        if admitted.source_id != connector:
            return _error("UNAUTHENTICATED", digest=digest, op=SCM_EVENT_OP)
        request_id = request.headers.get("x-request-id", "")[:128] or token_hex(12)
        try:
            caller = caller_from_session(
                session, credential_kind=SIGNED_CREDENTIAL_KIND, request_id=request_id
            )
        except PermissionError:
            return _error("UNAUTHENTICATED", digest=digest, op=SCM_EVENT_OP)
        params = body_params(
            connector, admitted.delivery_id, dict(request.headers), await request.body()
        )
        with caller_session_scope(caller):
            outcome = await invoke(
                SCM_EVENT_OP,
                params,
                caller,
                Surface.HTTP,
                services=services,
                idempotency_key=f"scm:{connector}:{admitted.delivery_id}"[:256],
            )
        return response(outcome, services.registry.get(SCM_EVENT_OP), request_id)

    scm_event.__name__ = "http_scm_event_intake"
    app.add_api_route(
        "/development/scm-events/{connector}",
        scm_event,
        methods=["POST"],
        include_in_schema=False,
    )


__all__ = ["SCM_EVENT_OP", "mount_scm_events_route"]
