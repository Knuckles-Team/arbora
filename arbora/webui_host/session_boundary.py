"""Host-owned browser admission around the public WebUI application factory."""

import inspect
from collections.abc import Callable
from typing import Any

from arbora.identity.ports import (
    BrowserAdmissionPort,
    IdentityUnavailable,
    SessionCookieReader,
)


class WebUISessionBoundary:
    """Install only with composition-owned admission and authentication routes.

    The injected auth application owns login/logout and issuance. This adapter
    never creates keys, tokens, session records, identities or permission grants.
    The admission preserves the exact live cookie-to-caller binding while the
    downstream WebUI independently verifies its forwarded bearer.

    Build it with the composition root's
    :func:`arbora.api.http.browser_app.create_webui_session_boundary`, which
    refuses any admission that is not the qualified concrete one. This package
    holds only the :class:`~arbora.identity.ports.BrowserAdmissionPort`.
    """

    identity_mode = "eg"

    def __init__(
        self,
        admission: BrowserAdmissionPort,
        auth_app: Callable[..., Any],
        *,
        operations: Any = None,
    ):
        if not callable(getattr(admission, "request", None)) or not callable(auth_app):
            raise IdentityUnavailable("qualified admission and auth routes required")
        self.admission = admission
        self.auth_app = auth_app
        self.operations = operations

    def __call__(self, app: Any) -> None:
        from arbora.api.http.browser_app import BrowserOperationApplication

        if (
            not isinstance(
                getattr(self, "operations", None), BrowserOperationApplication
            )
            or self.operations.admission is not self.admission
        ):
            raise IdentityUnavailable(
                "WebUI shared caller and invocation fence are not qualified"
            )
        from agent_webui.host import config

        # The WebUI factory has already resolved non-loopback defaults. Do not
        # reread Arbora's separate settings and accidentally disable its limit.
        self.operations.authenticator.configure_rate_policy(
            rate=float(config.gateway_rate_limit or 0),
            burst=float(config.gateway_rate_burst or 0),
        )
        app.add_middleware(
            CanonicalWebUIBoundaryMiddleware,
            operations=self.operations,
            auth_app=self.auth_app,
        )


class CanonicalWebUIBoundaryMiddleware:
    """A bounded host operation path; legacy protected routes stay unavailable.

    Security headers and Host validation installed later by the public WebUI
    factory still wrap this layer. The canonical sub-app performs its own
    exact-origin/CSRF admission and shared operation authorization.
    """

    def __init__(self, app: Any, *, operations: Any, auth_app: Any):
        from starlette.routing import Mount, Router

        self.app, self.auth_app = app, auth_app
        self.operations = Router(routes=[Mount("/api/v1", app=operations)])

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path == "/auth" or path.startswith("/auth/"):
            await self.auth_app(scope, receive, send)
            return
        if path == "/api/v1" or path.startswith("/api/v1/"):
            await self.operations(scope, receive, send)
            return
        from agent_webui.public_shell import public_shell_of

        shell = public_shell_of(scope)
        health = {"/health", "/health/ready", "/healthz", "/api/health", "/api/healthz"}
        if (
            kind == "http"
            and scope.get("method") in {"GET", "HEAD"}
            and (path in health or (shell is not None and shell.admits(scope)))
        ):
            # Retain the WebUI gate too: this layer never decides that a
            # dynamic route is public merely because its name looks static.
            await self.app(scope, receive, send)
            return
        # No legacy route may gain a grant from the host process, or from a
        # separately reminted WebUI identity. Migration is operation by operation.
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        from fastapi.responses import JSONResponse

        response = JSONResponse(
            {"error": "Legacy operation unavailable in host session mode"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


class BrowserSessionMiddleware:
    """Own /auth and wrap cookie requests without ambient authority fallback."""

    def __init__(
        self,
        app: Any,
        *,
        admission: BrowserAdmissionPort,
        auth_app: Any,
        read_session: SessionCookieReader,
    ):
        if not callable(read_session):
            raise IdentityUnavailable("injected session cookie reader required")
        self.app, self.admission, self.auth_app = app, admission, auth_app
        self.read_session = read_session

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path == "/auth" or path.startswith("/auth/"):
            await self.auth_app(scope, receive, send)
            return
        started = False

        async def tracked_send(message: Any) -> None:
            nonlocal started
            if message.get("type") in {"http.response.start", "websocket.accept"}:
                started = True
            await send(message)

        try:
            cookie = self.read_session(scope)
            if cookie is None:
                await self.app(scope, receive, tracked_send)
                return
            # The current qualified browser admission contract accepts HTTP,
            # not websocket handshake scopes. Refuse rather than relabel one.
            if kind == "websocket":
                raise PermissionError("browser websocket admission unavailable")
            async with self.admission.request(scope) as forwarded:
                await self.app(forwarded, receive, tracked_send)
        except (PermissionError, IdentityUnavailable) as exc:
            if started:
                raise
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 4401})
                return
            status = 503 if isinstance(exc, IdentityUnavailable) else 401
            body = (
                b'{"error":"Identity unavailable"}'
                if status == 503
                else b'{"error":"Authentication required"}'
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})


def identity_factory_kwargs(
    mode: str, boundary: WebUISessionBoundary | None, *, factory: Any = None
) -> dict[str, Any]:
    """Map explicit host configuration to the public WebUI factory contract."""
    if mode not in {"required", "none", "eg", "oidc"}:
        raise IdentityUnavailable("unknown WebUI identity mode")
    if mode == "none" and boundary is not None:
        raise IdentityUnavailable("no-auth mode conflicts with a session boundary")
    if mode == "eg" and boundary is None:
        raise IdentityUnavailable("EG mode requires qualified browser admission")
    if boundary is not None and not isinstance(boundary, WebUISessionBoundary):
        raise IdentityUnavailable("qualified host session boundary required")
    if mode != "required" and factory is not None:
        if "auth_mode" not in inspect.signature(factory).parameters:
            raise IdentityUnavailable("installed WebUI lacks explicit identity modes")
    kwargs: dict[str, Any] = {"auth_mode": mode} if mode != "required" else {}
    if boundary is not None:
        kwargs["session_boundary"] = boundary
    return kwargs
