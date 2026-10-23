"""One HTTP endpoint factory for generic and resource-style operations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from secrets import token_hex
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from arbora.api.registry import Invoke

from .auth import HTTPAuthenticationError


@contextmanager
def caller_session_scope(caller: Any) -> Iterator[None]:
    """Bind the authenticated caller's own verified session for one request.

    Handlers that reach the engine through the served client read the ambient
    ``GraphSession`` and actor, which the MCP transport binds per tool call.
    The REST surface binds the same authority here, from the session the
    request's bearer verified to -- never a process or synthesized one.
    """
    session = getattr(caller, "session", None)
    if session is None:
        yield None
        return
    from arbora.api.contracts.session import use_session
    from arbora.security.actor_context import use_actor

    with use_actor(session.actor), use_session(session):
        yield None


MAX_BODY_BYTES = 1_048_576
MAX_KEY_LENGTH = 256


def _error(code: str, *, digest: str, op: str, request_id: str = "") -> JSONResponse:
    from arbora.api.errors import ArboraErrorCode, ArboraRefusal, to_envelope

    status, envelope = to_envelope(
        ArboraRefusal(ArboraErrorCode(code)),
        op=op,
        request_id=request_id or token_hex(12),
        registry_digest=digest,
    )
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        # RFC 6750 section 3: a 401 names the scheme, as the MCP endpoint does.
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope, status_code=status, headers=headers)


async def _json_body_params(request: Request) -> dict[str, Any]:
    """Parse and bound a non-GET request's JSON object body."""

    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ValueError("JSON content type required")
    length = request.headers.get("content-length")
    if length is not None and int(length) > MAX_BODY_BYTES:
        raise ValueError("Body too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("Body too large")
    import json

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Object body required")
    return parsed


def _merge_resource_params(request: Request, data: dict[str, Any]) -> None:
    """Add a resource-style endpoint's query and path parameters in place."""

    for name, value in request.query_params.multi_items():
        if name in data or len(request.query_params.getlist(name)) != 1:
            raise ValueError("Duplicate parameter")
        data[name] = value
    for name, value in request.path_params.items():
        if name in data:
            raise ValueError("Duplicate parameter")
        data[name] = value


async def _params(request: Request, *, generic: bool) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if request.method != "GET":
        data.update(await _json_body_params(request))
    if not generic:
        _merge_resource_params(request, data)
    return data


def _invalid_header(value: str | None) -> bool:
    """A present idempotency or plan-ref header must be nonempty and bounded."""

    return value is not None and (not value or len(value) > MAX_KEY_LENGTH)


def _surface_for(
    request: Request,
    caller: Any,
    is_console_request: Callable[[Request, Any], bool] | None,
) -> Any:
    from arbora.api.registry import Surface

    if is_console_request is not None and is_console_request(request, caller):
        return Surface.CONSOLE
    return Surface.HTTP


def make_endpoint(
    op: Any,
    *,
    services: Any,
    authenticate: Callable[[Request], Awaitable[Any]],
    invoke: Invoke,
    response: Callable[[Any, Any, str], JSONResponse],
    generic: bool,
    is_console_request: Callable[[Request, Any], bool] | None = None,
) -> Callable[[Request], Awaitable[JSONResponse]]:
    """Return the single handler used by every registered HTTP operation.

    ``invoke`` is the single invocation chokepoint (ARBORA-OPS-R007),
    supplied by the server composition root; this module never imports it
    directly so it does not need that pipeline to exist to be importable.
    """

    async def endpoint(request: Request) -> JSONResponse:
        digest = services.registry.digest
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return _error("UNAUTHENTICATED", digest=digest, op=op.id)
        try:
            params = await _params(request, generic=generic)
        except (ValueError, TypeError):
            return _error("INVALID_ARGUMENT", digest=digest, op=op.id)
        key = request.headers.get("idempotency-key")
        plan_ref = request.headers.get("arbora-plan-ref")
        if _invalid_header(key) or _invalid_header(plan_ref):
            return _error("INVALID_ARGUMENT", digest=digest, op=op.id)
        with caller_session_scope(caller):
            outcome = await invoke(
                op.id,
                params,
                caller,
                _surface_for(request, caller, is_console_request),
                services=services,
                plan_ref=plan_ref,
                idempotency_key=key,
            )
        return response(outcome, op, caller.request_id)

    endpoint.__name__ = f"http_{op.id.replace('.', '_')}"
    return endpoint
