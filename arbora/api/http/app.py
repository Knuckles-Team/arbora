"""Standalone `/api/v1` application, mounted by the later cutover lane."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from secrets import token_hex
from types import MappingProxyType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from arbora.api.otlp_logs import body_key
from arbora.api.registry import Invoke
from arbora.core.settings import RuntimeConfig

from .auth import HTTPAuthenticationError
from .bundles import mount_bundle_transfer
from .checkout import make_checkout_endpoint
from .git_bridge import mount_git_bridge
from .lineage import mount_lineage_route
from .openapi import document
from .routes import error_response, make_endpoint
from .scm_events import mount_scm_events_route
from .sparql import mount_sparql_protocol

Visibility = Callable[[Any, Any], Awaitable[bool]]

# Operations whose protocol clients cannot send an Idempotency-Key are keyed
# by their body (ARBORA-OPS-R953).
_BODY_KEYED: Mapping[str, Callable[[dict[str, Any]], str]] = MappingProxyType(
    {
        "skills.telemetry.ingest": body_key,
    }
)


def _subapp_path(path: str) -> str:
    """Map a public v1 resource path to this app's mount-relative path."""
    if path.startswith("/api/v1/"):
        return path[len("/api/v1") :]
    if path.startswith("/") and not path.startswith("/api/"):
        return path
    raise ValueError(f"operation HTTP path is outside /api/v1: {path}")


#: Refusals whose reply tells the caller when to come back (R001.5 of the
#: adaptive-capacity spec: a throttled call retries after one second).
_RETRY_AFTER_S = MappingProxyType({"CAPACITY_THROTTLED": "1"})


def _refusal_headers(code: str) -> dict[str, str]:
    headers = {"Cache-Control": "no-store"}
    if code in _RETRY_AFTER_S:
        headers["Retry-After"] = _RETRY_AFTER_S[code]
    return headers


def _outcome_response(
    outcome: Any, op: Any, *, digest: str, request_id: str
) -> JSONResponse:
    from arbora.api.errors import to_envelope

    if hasattr(outcome, "code") and outcome.code != "OK":
        status, payload = to_envelope(
            outcome,
            op=op.id,
            request_id=request_id or token_hex(12),
            registry_digest=digest,
        )
        return JSONResponse(
            payload, status_code=status, headers=_refusal_headers(outcome.code)
        )
    value = getattr(outcome, "value", outcome)
    meta = {"registry_digest": digest, "api_version": "v1"}
    if hasattr(outcome, "meta") and isinstance(outcome.meta, dict):
        meta.update(outcome.meta)
    return JSONResponse(
        {"ok": True, "result": jsonable_encoder(value), "meta": meta},
        headers={"Cache-Control": "no-store"},
    )


def _mount_scim(app: FastAPI, scim_ports: Any) -> None:
    """ARBORA-IDENTITY-R011.2.4: the SCIM Users resource at its declared protocol path."""
    if scim_ports is None:
        return
    from arbora.identity.scim_routes import scim_app

    app.mount("/scim/v2", scim_app(scim_ports))


def create_api_application(
    *,
    services: Any,
    visibility: Visibility,
    authenticator: Any,
    invoke: Invoke,
    config: RuntimeConfig,
    scim_ports: Any = None,
) -> FastAPI:
    """Create an isolated sub-app with explicit shared authority dependencies.

    ``visibility`` must make the same policy decision as `invoke` for a caller.
    The composition root supplies ``authenticator`` (an object with
    ``authenticate(request)`` and ``is_console_request(request, caller)``)
    and ``invoke`` (ARBORA-OPS-R007's single invocation chokepoint); neither
    is constructed here so this module carries no dependency on the modules
    that build them. A missing visibility policy is never treated as an
    allow decision. No legacy Arbora route is mounted here.
    """
    from arbora.api.registry import Surface, canonical_op

    registry = services.registry
    auth = authenticator
    authenticate = auth.authenticate
    from .browser_auth import BoundBrowserHTTPAuthenticator, BrowserRateLimited

    operation_authority = (
        auth if isinstance(auth, BoundBrowserHTTPAuthenticator) else None
    )
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    _mount_scim(app, scim_ports)
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException

    @app.exception_handler(HTTPException)
    async def private_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        # A post-dispatch failure keeps its HTTP status, never becomes an auth
        # refusal. Do not reflect provider details or arbitrary response headers.
        headers = {"Cache-Control": "no-store"}
        if isinstance(exc, BrowserRateLimited):
            headers["Retry-After"] = str(exc.retry_after_seconds)
        return JSONResponse(
            {
                "detail": "Request failed"
                if exc.status_code < 500
                else "Internal request failed"
            },
            status_code=exc.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def private_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Request validation failed"},
            status_code=422,
            headers={"Cache-Control": "no-store"},
        )

    async def visible_for(request: Request, caller: Any) -> tuple[Any, ...]:
        allowed: dict[str, bool] = {}
        for op in registry:
            try:
                allowed[op.id] = bool(await visibility(op, caller))
            except Exception:  # fail closed on a policy service failure
                logging.getLogger(__name__).warning(
                    "authority call failed", exc_info=True
                )
                allowed[op.id] = False
        return registry.find(
            caller,
            surface=(
                Surface.CONSOLE
                if auth.is_console_request(request, caller)
                else Surface.HTTP
            ),
            policy=lambda op, _: allowed.get(op.id, False),
        )

    async def visible(request: Request) -> tuple[Any, ...] | JSONResponse:
        from arbora.identity.engine import IdentityUnavailable

        try:
            if operation_authority is not None:
                async with operation_authority.operation_scope(request) as caller:
                    result = await visible_for(request, caller)
                    await operation_authority.before_invocation(request, caller)
                    return result
            return await visible_for(request, await authenticate(request))
        except HTTPAuthenticationError:
            return error_response(
                "UNAUTHENTICATED", digest=registry.digest, op="api.registry"
            )
        except IdentityUnavailable:
            return error_response(
                "UNAVAILABLE", digest=registry.digest, op="api.registry"
            )

    @app.get("/registry", include_in_schema=False)
    async def registry_route(request: Request) -> JSONResponse:
        ops = await visible(request)
        if isinstance(ops, JSONResponse):
            return ops
        return JSONResponse(
            {
                "api_version": "v1",
                "registry_digest": registry.digest,
                "ops": [canonical_op(op) for op in ops],
            },
            headers={
                "ETag": f'"{registry.digest}"',
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/ops/{op_id}", include_in_schema=False)
    async def op_route(request: Request, op_id: str) -> JSONResponse:
        ops = await visible(request)
        if isinstance(ops, JSONResponse):
            return ops
        for op in ops:
            if op.id == op_id:
                return JSONResponse(
                    canonical_op(op),
                    headers={
                        "ETag": f'"{registry.digest}"',
                        "Cache-Control": "private, no-store",
                    },
                )
        return error_response("UNKNOWN_OP", digest=registry.digest, op=op_id)

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi_route(request: Request) -> JSONResponse:
        ops = await visible(request)
        if isinstance(ops, JSONResponse):
            return ops
        return JSONResponse(
            document(ops, digest=registry.digest),
            headers={
                "ETag": f'"{registry.digest}"',
                "Cache-Control": "private, no-store",
            },
        )

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui_route(request: Request) -> HTMLResponse:
        # Resolve the sibling ``openapi.json`` route relative to wherever this
        # sub-app is mounted, so the rendered page never hardcodes a mount
        # prefix (ARBORA-OPS-R038).
        openapi_url = f"{request.url.path.rsplit('/', 1)[0]}/openapi.json"
        return get_swagger_ui_html(
            openapi_url=openapi_url, title="Arbora API — Swagger UI"
        )

    for op in registry:
        if Surface.HTTP not in op.surfaces and Surface.CONSOLE not in op.surfaces:
            continue
        app.add_api_route(
            f"/ops/{op.id}",
            make_endpoint(
                op,
                services=services,
                authenticate=authenticate,
                invoke=invoke,
                response=lambda result, current_op, request_id: _outcome_response(
                    result, current_op, digest=registry.digest, request_id=request_id
                ),
                generic=True,
                is_console_request=auth.is_console_request,
                operation_authority=operation_authority,
            ),
            methods=["POST"],
            include_in_schema=False,
        )
        if op.http is not None:
            app.add_api_route(
                _subapp_path(op.http.path),
                make_endpoint(
                    op,
                    services=services,
                    authenticate=authenticate,
                    invoke=invoke,
                    response=lambda result, current_op, request_id: _outcome_response(
                        result,
                        current_op,
                        digest=registry.digest,
                        request_id=request_id,
                    ),
                    generic=False,
                    is_console_request=auth.is_console_request,
                    operation_authority=operation_authority,
                    derive_key=_BODY_KEYED.get(op.id),
                ),
                methods=[str(op.http.method).upper()],
                include_in_schema=False,
            )

    # A signed code-host delivery is admitted by the identity middleware and served
    # through the same invoke chokepoint (ARBORA-OPS-R896.8).
    mount_scm_events_route(
        app,
        services=services,
        invoke=invoke,
        response=lambda result, current_op, request_id: _outcome_response(
            result, current_op, digest=registry.digest, request_id=request_id
        ),
    )

    # The virtual checkout streams one repository ref as NDJSON by driving
    # the admitted ``source.tree`` operation through the same invoke
    # chokepoint (ARBORA-OPS-R850); it is an endpoint, not a registry op,
    # because a stream has no single JSON envelope.
    async def browser_checkout_unavailable(request: Request) -> JSONResponse:
        # A StreamingResponse executes outside the endpoint's task/lifetime.
        # Do not reuse HTTP-bound authority until a streaming contract exists.
        return error_response(
            "UNAVAILABLE", digest=registry.digest, op="source.checkout"
        )

    app.add_api_route(
        "/source/checkout",
        browser_checkout_unavailable
        if operation_authority is not None
        else make_checkout_endpoint(
            services=services,
            authenticate=authenticate,
            invoke=invoke,
            is_console_request=auth.is_console_request,
        ),
        methods=["GET"],
        include_in_schema=False,
    )
    # The SPARQL 1.1 Protocol and Graph Store endpoints drive query.sparql
    # and eg.graph.ApplyMutation through the same invoke chokepoint
    # (ARBORA-OPS-R857.1).
    mount_bundle_transfer(
        app,
        services=services,
        authenticate=authenticate,
        invoke=invoke,
        is_console_request=auth.is_console_request,
    )
    mount_git_bridge(
        app,
        services=services,
        authenticate=authenticate,
        invoke=invoke,
        is_console_request=auth.is_console_request,
    )
    mount_sparql_protocol(
        app,
        services=services,
        authenticate=authenticate,
        invoke=invoke,
        is_console_request=auth.is_console_request,
        max_result_bytes=config.sparql_max_result_bytes,
    )
    # OpenLineage run events drive eg.ingestion.LineageIngest through the same
    # chokepoint (ARBORA-OPS-R889).
    mount_lineage_route(
        app,
        services=services,
        authenticate=authenticate,
        invoke=invoke,
        is_console_request=auth.is_console_request,
    )
    return app


def create_run_compatibility_application(
    *, services: Any, authenticator: Any, invoke: Invoke
) -> FastAPI:
    """Legacy run URLs use the exact registry/auth/invoke path, never a bypass."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    def response(outcome: Any, op: Any, request_id: str) -> JSONResponse:
        if hasattr(outcome, "code") and outcome.code != "OK":
            return _outcome_response(
                outcome, op, digest=services.registry.digest, request_id=request_id
            )
        value = getattr(outcome, "value", outcome)
        return JSONResponse(
            jsonable_encoder(value["value"]), headers={"Cache-Control": "no-store"}
        )

    operations = {op.id: op for op in services.registry}
    for action, path in (("get", "/{run_id}"), ("events", "/{run_id}/events")):
        op = operations.get(f"runs.{action}")
        if op is None:
            continue
        app.add_api_route(
            path,
            make_endpoint(
                op,
                services=services,
                authenticate=authenticator.authenticate,
                invoke=invoke,
                response=response,
                generic=False,
                is_console_request=authenticator.is_console_request,
            ),
            methods=["GET"],
            include_in_schema=False,
        )
    return app
