"""Native human browser sessions; no principal, key or permission provisioning."""

import asyncio
import threading
import time
from typing import Any

from starlette.responses import JSONResponse

from .engine import IdentityUnavailable
from .issuer import require_token_binding
from .ports import SessionState


class _LoginResponse(JSONResponse):
    """Retain only this newly issued cookie for failed-delivery cleanup."""

    _created_session: str


class BrowserCallerResolver:
    """Combine verified bearer facts with fresh credential-derived EG authority."""

    def __init__(self, authority: Any, verify_token: Any, *, graph: str):
        self.authority, self.verify_token, self.graph = authority, verify_token, graph

    async def __call__(self, token: str, credential: str) -> Any:
        from arbora.api.contracts.session import GraphSession
        from arbora.security.actor_context import ActorContext, ActorType

        verified = await self.verify_token(token)
        # Resolve after verification's possible JWKS await. No user lookup or
        # JWT actor helper can substitute for this live session resolution.
        state = await self.authority.resolve_session(credential)
        if not isinstance(state, SessionState):
            raise IdentityUnavailable("persisted browser session required")
        resolution = state.resolution
        if resolution.kind != "human" or resolution.is_bootstrap:
            raise PermissionError("human browser session required")
        require_token_binding(verified.claims, resolution)
        expiry = verified.claims.get("exp")
        if (
            type(expiry) is not int
            or expiry * 1000 > state.expires_at_ms
            or expiry <= time.time()
        ):
            raise PermissionError("browser token lifetime disagrees")
        context = resolution.request_context
        session = GraphSession(
            actor=ActorContext(
                actor_id=resolution.principal_id,
                actor_type=ActorType.HUMAN,
                roles=resolution.roles,
                groups=resolution.groups,
                tenant_id=context["tenant"],
                authenticated=True,
                credential_expires_at=expiry,
            ),
            tenant=context["tenant"],
            scopes=frozenset(resolution.scopes),
            graph=self.graph,
            audience=context["audience"],
            policy_version=context["policy_version"],
        )
        # Also detects an ambient delegated context; browser authority must be
        # exact and is never inherited from the supervising process.
        from .broker import _require_caller

        _require_caller(session, state)
        return session


class BrowserAuthApplication:
    """Bounded native password login/logout, not a second identity database.

    Login generates only an opaque session token for EG's authenticate operation.
    No user, signing key, role or permission is created. Unsupported MFA/password
    change ceremonies refuse; they never expose a pending session cookie.
    """

    def __init__(
        self,
        client: Any,
        authority: Any,
        *,
        graph: str,
        origin: str,
        issuer: Any,
        caller: Any,
    ):
        self.client, self.authority = client, authority
        self.graph, self.origin = graph, origin
        self.issuer, self.caller = issuer, caller
        self._in_flight = 0
        self._capacity_lock = threading.Lock()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from starlette.responses import JSONResponse

        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        if scope.get("type") != "http":
            raise IdentityUnavailable("HTTP browser auth required")
        with self._capacity_lock:
            admitted = self._in_flight < 16
            if admitted:
                self._in_flight += 1
        if not admitted:
            await JSONResponse(
                {"error": "Identity unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        response = None
        try:
            try:
                async with asyncio.timeout(10):
                    response = await self._response(scope, receive)
            except PermissionError:
                response = JSONResponse(
                    {"error": "Authentication required"}, status_code=401
                )
            except (IdentityUnavailable, TimeoutError):
                response = JSONResponse(
                    {"error": "Identity unavailable"}, status_code=503
                )
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)
        except BaseException:
            credential = getattr(response, "_created_session", None)
            if credential is not None:
                await self._revoke(credential)
            raise
        finally:
            with self._capacity_lock:
                self._in_flight -= 1

    async def _response(self, scope: Any, receive: Any) -> Any:
        from urllib.parse import urlsplit

        from starlette.responses import Response

        from .browser import _headers

        if scope.get("method") == "GET" and scope.get("path") == "/auth/session":
            return await self._session(scope)
        if scope.get("method") != "POST" or scope.get("path") not in {
            "/auth/login",
            "/auth/logout",
        }:
            return Response(status_code=404)
        if (
            scope.get("scheme") != "https"
            or _headers(scope, b"origin") != (self.origin.encode("ascii"),)
            or _headers(scope, b"host")
            != (urlsplit(self.origin).netloc.encode("ascii"),)
            or _headers(scope, b"authorization")
        ):
            raise PermissionError("exact secure browser origin required")
        if scope["path"] == "/auth/logout":
            return await self._logout(scope)
        return await self._login(scope, await _login_body(scope, receive))

    async def _logout(self, scope: Any) -> Any:
        from starlette.responses import Response

        from .approval_engine import identity_value
        from .browser import require_mutation_proof, session_from_scope

        require_mutation_proof(scope, trusted_origin=self.origin)
        await identity_value(
            self.client,
            self.graph,
            {
                "family": "session",
                "op": "revoke",
                "request": {"session_token": session_from_scope(scope)},
            },
            "done",
        )
        response = Response(status_code=204)
        _clear_cookie(response)
        return response

    async def _login(self, scope: Any, body: dict[str, str]) -> Any:
        import secrets

        from .approval_engine import identity_value
        from .browser import csrf_token_for, session_cookie_header

        credential = secrets.token_urlsafe(32)
        try:
            result = await identity_value(
                self.client,
                self.graph,
                {
                    "family": "credential",
                    "op": "authenticate",
                    "request": {
                        **body,
                        "session_token": credential,
                        "ip_prefix": _peer_prefix(scope),
                    },
                },
                "authenticate",
            )
            if not isinstance(result, dict) or result.get("outcome") != "ok":
                raise PermissionError("authentication ceremony incomplete")
            # Do not emit a cookie unless the existing signing/verifier inputs
            # and current caller conversion actually agree. Never return JWTs.
            token = await self.issuer.issue(credential)
            await self.caller(token, credential)
            state = await self.authority.resolve_session(credential)
            if not isinstance(state, SessionState) or state.resolution.kind != "human":
                raise PermissionError("human browser session required")
            response = _LoginResponse(
                {"outcome": "ok", "csrf_token": csrf_token_for(credential)}
            )
            response.headers["Set-Cookie"] = session_cookie_header(
                credential,
                expires_at_ms=state.expires_at_ms,
                now_ms=int(time.time() * 1000),
            ).decode("ascii")
            response._created_session = credential
            return response
        except BaseException:
            # Authentication may have persisted a session before its response
            # was lost. Revoke this exact random token, never all user sessions.
            # A failed revoke remains an unknown effect, not claimed rollback.
            await self._revoke(credential)
            raise

    async def _revoke(self, credential: str) -> None:
        from .approval_engine import identity_value

        try:
            await identity_value(
                self.client,
                self.graph,
                {
                    "family": "session",
                    "op": "revoke",
                    "request": {"session_token": credential},
                },
                "done",
            )
        except (IdentityUnavailable, PermissionError):
            import logging

            logging.getLogger(__name__).warning(
                "Browser authentication cleanup outcome unavailable"
            )

    async def _session(self, scope: Any) -> Any:
        from urllib.parse import urlsplit

        from starlette.responses import JSONResponse

        from .browser import (
            _headers,
            csrf_token_for,
            require_mutation_proof,
            session_from_scope,
        )

        origins = _headers(scope, b"origin")
        if (
            scope.get("scheme") != "https"
            or _headers(scope, b"host")
            != (urlsplit(self.origin).netloc.encode("ascii"),)
            or _headers(scope, b"authorization")
            or (origins and origins != (self.origin.encode("ascii"),))
        ):
            raise PermissionError("exact secure browser transport required")
        try:
            credential = session_from_scope(scope)
            if credential is None:
                # Presentation only, necessary to render the existing password
                # form. No principal, tenant, CSRF proof or grant is exposed.
                return JSONResponse(
                    {
                        "authenticated": False,
                        "mode": "local",
                        "roles": [],
                        "webui_role": None,
                    }
                )
            require_mutation_proof(scope, trusted_origin=self.origin)
            token = await self.issuer.issue(credential)
            session = await self.caller(token, credential)
        except PermissionError:
            response = JSONResponse(
                {"error": "Authentication required"}, status_code=401
            )
            _clear_cookie(response)
            return response
        try:
            from overstory.rbac import resolve_webui_role
        except ImportError:
            raise IdentityUnavailable(
                "WebUI presentation role contract unavailable"
            ) from None
        return JSONResponse(
            {
                "authenticated": True,
                "mode": "local",
                "csrf_token": csrf_token_for(credential),
                "subject": session.actor.actor_id,
                "tenant": session.tenant,
                "roles": list(session.actor.roles),
                # Presentation only: canonical dispatch never authorizes by UI role.
                "webui_role": resolve_webui_role(
                    session.actor.roles, authenticated=True
                ),
            }
        )


async def _login_body(scope: Any, receive: Any) -> dict[str, str]:
    import json

    from .browser import _headers, session_from_scope

    if session_from_scope(scope) is not None:
        raise PermissionError("existing session must be logged out first")
    if _headers(scope, b"content-type") != (b"application/json",):
        raise PermissionError("JSON login required")
    data = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            raise PermissionError("login interrupted")
        data.extend(message.get("body", b""))
        if len(data) > 4096:
            raise PermissionError("login body too large")
        if not message.get("more_body"):
            break
    try:
        body = json.loads(data, object_pairs_hook=_unique_login_fields)
    except (ValueError, UnicodeError):
        raise PermissionError("invalid login") from None
    if (
        type(body) is not dict
        or set(body) != {"username", "password"}
        or any(type(body[name]) is not str or not body[name] for name in body)
    ):
        raise PermissionError("invalid login")
    return body


def _clear_cookie(response: Any) -> None:
    from .browser import SESSION_COOKIE

    response.headers["Set-Cookie"] = (
        f"{SESSION_COOKIE}=; Max-Age=0; Secure; HttpOnly; SameSite=Lax; Path=/"
    )


def _unique_login_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate login field")
        result[name] = value
    return result


def _peer_prefix(scope: Any) -> str | None:
    """Native throttle input from ASGI peer only, never forwarded headers."""
    import ipaddress

    peer = scope.get("client")
    if not peer:
        return None
    try:
        address = ipaddress.ip_address(peer[0])
        prefix = 24 if address.version == 4 else 48
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
    except (ValueError, TypeError, IndexError):
        raise PermissionError("invalid browser peer") from None
