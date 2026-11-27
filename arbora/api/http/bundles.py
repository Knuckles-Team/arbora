"""Knowledge bundle transfer routes (ARBORA-OPS-R923.2, R923.3).

``GET /api/v1/bundles/{bundle_ref}`` streams a staged bundle and
``PUT /api/v1/bundles/{bundle_ref}`` stages an uploaded one. Both authenticate through
the shared HTTP auth and drive ``bundle.download`` and ``bundle.upload`` through the
invocation chokepoint, so admission, scope and audit are the same as any operation call.
Arbora holds no bundle bytes: every chunk passes straight between the engine blob store
and the socket. A declared or streamed body over 4 GiB is refused ``BUNDLE_TOO_LARGE``
(HTTP 413) before the stage is committed. A reference of another tenant answers exactly
like an absent one: the ``NOT_FOUND`` envelope with the same body.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from secrets import token_hex
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from arbora.api.errors import ArboraErrorCode, ArboraRefusal, to_envelope
from arbora.api.ops.bundle import CHUNK_BYTES, MAX_BUNDLE_BYTES

from .auth import HTTPAuthenticationError
from .routes import _surface_for, caller_session_scope

_DOWNLOAD = "bundle.download"
_UPLOAD = "bundle.upload"


def _respond(outcome: Any, *, op: str, digest: str, request_id: str) -> Response:
    status, envelope = to_envelope(
        outcome, op=op, request_id=request_id or token_hex(12), registry_digest=digest
    )
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope, status_code=status, headers=headers)


def _code(code: str, *, op: str, digest: str, request_id: str = "") -> Response:
    return _respond(
        ArboraRefusal(ArboraErrorCode(code)),
        op=op,
        digest=digest,
        request_id=request_id,
    )


def _admitted(outcome: Any) -> bool:
    return not hasattr(outcome, "source") and getattr(outcome, "code", "OK") == "OK"


def _value(outcome: Any) -> dict[str, Any]:
    value = getattr(outcome, "value", outcome)
    inner = value.get("value") if isinstance(value, dict) else None
    return (
        inner if isinstance(inner, dict) else (value if isinstance(value, dict) else {})
    )


def mount_bundle_transfer(
    app: FastAPI,
    *,
    services: Any,
    authenticate: Any,
    invoke: Any,
    is_console_request: Any = None,
) -> None:
    """Add the two bundle transfer routes to ``app``; ``invoke`` is the one chokepoint."""
    digest = services.registry.digest

    async def step(
        request: Request, caller: Any, op: str, params: dict[str, Any], key: str | None
    ) -> Any:
        surface = _surface_for(request, caller, is_console_request)
        with caller_session_scope(caller):
            return await invoke(
                op, params, caller, surface, services=services, idempotency_key=key
            )

    async def fetch(request: Request, bundle_ref: str) -> Response:
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return _code("UNAUTHENTICATED", op=_DOWNLOAD, digest=digest)
        first = await step(
            request, caller, _DOWNLOAD, {"bundle_ref": bundle_ref, "index": 0}, None
        )
        if not _admitted(first):
            return _respond(
                first, op=_DOWNLOAD, digest=digest, request_id=caller.request_id
            )
        total = int(_value(first).get("chunks", 0))

        async def body() -> AsyncIterator[bytes]:
            outcome = first
            for index in range(total):
                if index:
                    outcome = await step(
                        request,
                        caller,
                        _DOWNLOAD,
                        {"bundle_ref": bundle_ref, "index": index},
                        None,
                    )
                    if not _admitted(outcome):
                        raise RuntimeError(
                            "bundle chunk refused after the stream began"
                        )
                yield base64.b64decode(_value(outcome).get("data_b64", ""))

        return StreamingResponse(
            body(),
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    async def stage(request: Request, bundle_ref: str) -> Response:
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return _code("UNAUTHENTICATED", op=_UPLOAD, digest=digest)
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BUNDLE_BYTES:
            return _code(
                "BUNDLE_TOO_LARGE",
                op=_UPLOAD,
                digest=digest,
                request_id=caller.request_id,
            )
        base = request.headers.get("idempotency-key") or token_hex(16)

        async def upload(phase: str, n: int, **fields: Any) -> Any:
            return await step(
                request, caller, _UPLOAD, {"phase": phase, **fields}, f"{base}:{n}"
            )

        begun = await upload("begin", 0)
        if not _admitted(begun):
            return _respond(
                begun, op=_UPLOAD, digest=digest, request_id=caller.request_id
            )
        cursor = _value(begun)["cursor"]
        size, n = 0, 0
        async for chunk in _chunks(request):
            size += len(chunk)
            if size > MAX_BUNDLE_BYTES:
                return _code(
                    "BUNDLE_TOO_LARGE",
                    op=_UPLOAD,
                    digest=digest,
                    request_id=caller.request_id,
                )
            n += 1
            data = base64.b64encode(chunk).decode("ascii")
            put = await upload("chunk", n, cursor=cursor, data_b64=data)
            if not _admitted(put):
                return _respond(
                    put, op=_UPLOAD, digest=digest, request_id=caller.request_id
                )
        sealed = await upload("commit", n + 1, cursor=cursor)
        if not _admitted(sealed):
            return _respond(
                sealed, op=_UPLOAD, digest=digest, request_id=caller.request_id
            )
        return JSONResponse(
            {"bundle_ref": _value(sealed)["bundle_ref"]},
            headers={"Cache-Control": "no-store"},
        )

    app.add_api_route(
        "/bundles/{bundle_ref}", fetch, methods=["GET"], include_in_schema=False
    )
    app.add_api_route(
        "/bundles/{bundle_ref}", stage, methods=["PUT"], include_in_schema=False
    )


async def _chunks(request: Request) -> AsyncIterator[bytes]:
    """The request body in transfer-sized pieces."""
    pending = b""
    async for piece in request.stream():
        pending += piece
        while len(pending) >= CHUNK_BYTES:
            yield pending[:CHUNK_BYTES]
            pending = pending[CHUNK_BYTES:]
    if pending:
        yield pending


__all__ = ["mount_bundle_transfer"]
