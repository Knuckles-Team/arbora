"""SCIM ``Users`` routes (ARBORA-IDENTITY-R011.2.4).

Mounted at ``/scim/v2`` (the ``PROTOCOL_ROUTES`` path). Each request passes
``check_scim_credential`` first; the credential binds one provider id, so a token for
another provider answers 403. Responses use the SCIM error schema. One audit event per
call holds the provider id and a digest of the user id, never a token. Arbora keeps no
user store: every effect goes through the injected :class:`ScimIdentityPort`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from arbora.identity.engine import IdentityUnavailable
from arbora.identity.scim import (
    ScimCredentialRefused,
    ScimIdentityPort,
    ScimServiceCredential,
    check_scim_credential,
    scim_create_user,
    scim_deactivate_user,
    scim_patch_user,
    scim_update_user,
)

__all__ = ["ScimPorts", "scim_app"]

ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_MAX_BODY = 64 * 1024


@dataclass(frozen=True, slots=True)
class ScimPorts:
    """The credential map (bearer token to provider-scoped credential) and the port."""

    credentials: Mapping[str, ScimServiceCredential]
    identity: ScimIdentityPort
    emit: Callable[[Mapping[str, str]], None] = field(default=lambda event: None)


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {
            "schemas": [ERROR_SCHEMA],
            "status": str(status),
            "scimType": code,
            "detail": detail,
        },
        status_code=status,
        media_type="application/scim+json",
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _credential_refusal(
    ports: ScimPorts, token: str | None, provider: str, refused: Exception
) -> JSONResponse:
    known = token in ports.credentials if token else False
    status, code = (
        (403, "SCIM_PROVIDER_MISMATCH") if known else (401, "SCIM_CREDENTIAL_INVALID")
    )
    ports.emit({"op": "scim", "provider": provider, "outcome": code})
    return _error(status, code, str(refused) if known else "credential invalid")


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return (
        header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else None
    )


async def _body(request: Request) -> dict[str, Any]:
    if request.method not in {"POST", "PUT", "PATCH"}:
        return {}
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        raise IdentityUnavailable("SCIM body too large")
    body = json.loads(raw or b"{}")
    if not isinstance(body, dict):
        raise IdentityUnavailable("SCIM body must be an object")
    return body


async def _settled(result: Any) -> Any:
    """A port may be synchronous or an engine-backed coroutine; both are served."""
    return await result if inspect.isawaitable(result) else result


async def _created(
    port: ScimIdentityPort, user_id: str, body: dict[str, Any]
) -> Response:
    return JSONResponse(
        {"id": await _settled(scim_create_user(port, body))},
        status_code=201,
        media_type="application/scim+json",
    )


async def _updated(
    port: ScimIdentityPort, user_id: str, body: dict[str, Any]
) -> Response:
    await _settled(scim_update_user(port, user_id, body))
    return JSONResponse({"id": user_id}, media_type="application/scim+json")


async def _patched(
    port: ScimIdentityPort, user_id: str, body: dict[str, Any]
) -> Response:
    await _settled(scim_patch_user(port, user_id, body))
    return JSONResponse({"id": user_id}, media_type="application/scim+json")


async def _deactivated(
    port: ScimIdentityPort, user_id: str, body: dict[str, Any]
) -> Response:
    await _settled(scim_deactivate_user(port, user_id))
    return Response(status_code=204)


#: (method, has user id) to the handler and its outcome name.
_DISPATCH: dict[tuple[str, bool], tuple[Callable[..., Awaitable[Response]], str]] = {
    ("POST", False): (_created, "created"),
    ("PUT", True): (_updated, "updated"),
    ("PATCH", True): (_patched, "patched"),
    ("DELETE", True): (_deactivated, "deactivated"),
}


def scim_app(ports: ScimPorts) -> Starlette:
    async def handle(request: Request) -> Response:
        provider = request.path_params["provider_id"]
        user_id = request.path_params.get("user_id", "")
        token = _bearer(request)
        try:
            check_scim_credential(ports.credentials, token, provider)
        except ScimCredentialRefused as refused:
            return _credential_refusal(ports, token, provider, refused)
        route = _DISPATCH.get((request.method, bool(user_id)))
        if route is None:
            return _error(405, "SCIM_METHOD_NOT_ALLOWED", "method not allowed")
        action, outcome = route
        try:
            response = await action(ports.identity, user_id, await _body(request))
        except (IdentityUnavailable, ValueError):
            ports.emit(
                {"op": "scim", "provider": provider, "outcome": "SCIM_USER_INVALID"}
            )
            return _error(400, "SCIM_USER_INVALID", "user resource invalid")
        ports.emit(
            {
                "op": "scim",
                "provider": provider,
                "user": _digest(user_id) if user_id else "",
                "outcome": outcome,
            }
        )
        return response

    return Starlette(
        routes=[
            Route("/{provider_id}/Users", handle, methods=["POST"]),
            Route(
                "/{provider_id}/Users/{user_id}",
                handle,
                methods=["PUT", "PATCH", "DELETE"],
            ),
        ]
    )
