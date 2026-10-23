"""Inactive composition primitive: admit after ASGI mount routing settles."""

from typing import Any

from arbora.identity.admission import BrowserAdmission
from arbora.identity.engine import IdentityUnavailable

from .app import create_api_application
from .browser_auth import BoundBrowserHTTPAuthenticator
from .routes import _error


class BrowserOperationApplication:
    """A canonical operation sub-app, not a replacement WebUI identity gate."""

    def __init__(
        self,
        app: Any,
        admission: BrowserAdmission,
        digest: str,
        *,
        max_in_flight: int = 16,
        authenticator: BoundBrowserHTTPAuthenticator,
    ):
        import threading

        if type(max_in_flight) is not int or not 1 <= max_in_flight <= 1024:
            raise ValueError("bounded browser operation capacity required")
        self.app, self.admission, self.digest = app, admission, digest
        self.authenticator = authenticator
        self._capacity = max_in_flight
        self._in_flight = 0
        self._capacity_lock = threading.Lock()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._serve(scope, receive, send)
            return
        with self._capacity_lock:
            admitted = self._in_flight < self._capacity
            if admitted:
                self._in_flight += 1
        if not admitted:
            response = _error("UNAVAILABLE", digest=self.digest, op="api.browser")
            await response(scope, receive, send)
            return
        try:
            await self._serve(scope, receive, send)
        finally:
            with self._capacity_lock:
                self._in_flight -= 1

    async def _serve(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        if scope.get("type") != "http":
            raise RuntimeError("unsupported browser operation transport")
        from fastapi import HTTPException
        from fastapi.responses import JSONResponse

        from .routes import MAX_BODY_BYTES

        lengths = [
            v for k, v in scope.get("headers", ()) if k.lower() == b"content-length"
        ]
        transfers = [
            v for k, v in scope.get("headers", ()) if k.lower() == b"transfer-encoding"
        ]
        invalid = (
            len(lengths) > 1
            or len(transfers) > 1
            or bool(lengths and transfers)
            or any(not value.isdigit() or len(value) > 20 for value in lengths)
            or any(value.lower() != b"chunked" for value in transfers)
        )
        oversized = bool(lengths and not invalid and int(lengths[0]) > MAX_BODY_BYTES)
        if invalid or oversized:
            response = JSONResponse(
                {"detail": "Request rejected"},
                status_code=400 if invalid else 413,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    # The canonical app's own privacy handler catches this,
                    # before its server-error middleware could emit a 500.
                    raise HTTPException(status_code=413, detail="Request rejected")
            return message

        # Catch only admission-entry failures. Exceptions after dispatch may
        # describe unknown effect outcomes and must not become auth refusals.
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as lifetime:
            try:
                forwarded = await lifetime.enter_async_context(
                    self.admission.request(scope)
                )
            except PermissionError:
                response = _error(
                    "UNAUTHENTICATED", digest=self.digest, op="api.browser"
                )
                await response(scope, receive, send)
                return
            except IdentityUnavailable:
                response = _error("UNAVAILABLE", digest=self.digest, op="api.browser")
                await response(scope, receive, send)
                return
            await self.app(forwarded, bounded_receive, send)


def create_browser_api_application(
    *,
    admission: BrowserAdmission,
    services: Any,
    visibility: Any,
    invoke: Any,
    max_in_flight: int = 16,
) -> Any:
    """Compose a cookie-only canonical sub-app with one exact authority owner.

    Mount this returned ASGI application at the final operation prefix. Never
    put admission outside another Mount: root-path mutation invalidates its
    immutable request binding. Existing WebUI local routes are not authorized
    by this application, and no issuer or deployment is created here.
    """
    if not isinstance(admission, BrowserAdmission):
        raise IdentityUnavailable("qualified browser admission required")
    auth = BoundBrowserHTTPAuthenticator(
        admission.browser_authority, console_origin=admission.trusted_origin
    )
    app = create_api_application(
        services=services, visibility=visibility, authenticator=auth, invoke=invoke
    )
    return BrowserOperationApplication(
        app,
        admission,
        services.registry.digest,
        max_in_flight=max_in_flight,
        authenticator=auth,
    )
