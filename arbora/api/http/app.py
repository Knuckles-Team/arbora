"""Standalone `/api/v1` application, mounted by the later cutover lane."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from secrets import token_hex
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from arbora.api.registry import Invoke

from .auth import HTTPAuthenticationError
from .checkout import make_checkout_endpoint
from .openapi import document
from .routes import _error, make_endpoint

Visibility = Callable[[Any, Any], Awaitable[bool]]


def _subapp_path(path: str) -> str:
    """Map a public v1 resource path to this app's mount-relative path."""
    if path.startswith("/api/v1/"):
        return path[len("/api/v1") :]
    if path.startswith("/") and not path.startswith("/api/"):
        return path
    raise ValueError(f"operation HTTP path is outside /api/v1: {path}")


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
            payload, status_code=status, headers={"Cache-Control": "no-store"}
        )
    value = getattr(outcome, "value", outcome)
    meta = {"registry_digest": digest, "api_version": "v1"}
    if hasattr(outcome, "meta") and isinstance(outcome.meta, dict):
        meta.update(outcome.meta)
    return JSONResponse(
        {"ok": True, "result": jsonable_encoder(value), "meta": meta},
        headers={"Cache-Control": "no-store"},
    )


def create_api_application(
    *,
    services: Any,
    visibility: Visibility,
    authenticator: Any,
    invoke: Invoke,
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
            return _error("UNAUTHENTICATED", digest=registry.digest, op="api.registry")
        except IdentityUnavailable:
            return _error("UNAVAILABLE", digest=registry.digest, op="api.registry")

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
        return _error("UNKNOWN_OP", digest=registry.digest, op=op_id)

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
                ),
                methods=[str(op.http.method).upper()],
                include_in_schema=False,
            )

    # The virtual checkout streams one repository ref as NDJSON by driving
    # the admitted ``source.tree`` operation through the same invoke
    # chokepoint (ARBORA-OPS-R850); it is an endpoint, not a registry op,
    # because a stream has no single JSON envelope.
    async def browser_checkout_unavailable(request: Request) -> JSONResponse:
        # A StreamingResponse executes outside the endpoint's task/lifetime.
        # Do not reuse HTTP-bound authority until a streaming contract exists.
        return _error("UNAVAILABLE", digest=registry.digest, op="source.checkout")

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
    return app
