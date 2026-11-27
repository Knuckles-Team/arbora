"""Native human browser sessions; no principal, key or permission provisioning."""

import asyncio
import threading
import time
from typing import Any

from starlette.responses import JSONResponse, RedirectResponse

from .engine import IdentityUnavailable
from .issuer import require_token_binding
from .ports import SessionState


class _LoginResponse(JSONResponse):
    """Retain only this newly issued cookie for failed-delivery cleanup."""

    _created_session: str


class _LoginRedirect(RedirectResponse):
    """A federated sign-in landing: the new cookie rides a redirect to the console."""

    _created_session: str


class BrowserCallerResolver:
    """Combine verified bearer facts with fresh credential-derived EG authority."""

    def __init__(self, authority: Any, verify_token: Any, *, graph: str):
        self.authority, self.verify_token, self.graph = authority, verify_token, graph

    async def __call__(self, token: str, credential: str) -> Any:
        from arbora.security.actor_context import ActorContext, ActorType
        from arbora.security.session import GraphSession

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
        discovery: Any = None,
        audit: Any = None,
        acting: Any = None,
        mail: Any = None,
        federation: Any = None,
    ):
        from .account_ceremonies import AccountCeremonies
        from .mfa_ceremonies import MfaCeremonies, log_audit

        sink = audit or log_audit
        self.ceremonies = MfaCeremonies(client, graph, origin, sink)
        self.accounts = AccountCeremonies(
            client, graph, acting=acting, audit=sink, mail=mail
        )
        self.discovery = discovery
        self.federation = federation
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
        answered = self._published(scope) or await self._federated(scope, receive)
        if answered is not None:
            return answered
        routes = self._routes()
        if scope.get("method") != "POST" or scope.get("path") not in routes:
            return Response(status_code=404)
        if (
            scope.get("scheme") != "https"
            or _headers(scope, b"origin") != (self.origin.encode("ascii"),)
            or _headers(scope, b"host")
            != (urlsplit(self.origin).netloc.encode("ascii"),)
            or _headers(scope, b"authorization")
        ):
            raise PermissionError("exact secure browser origin required")
        return await routes[scope["path"]](scope, receive)

    def _routes(self) -> dict[str, Any]:
        """Every POST route of ``/auth/*`` and the handler that answers it."""
        from .account_ceremonies import ACCOUNT_ROUTES
        from .mfa_ceremonies import CEREMONY_ROUTES
        from .mfa_totp import TOTP_ROUTES

        routes: dict[str, Any] = {
            "/auth/login": self._login_route,
            "/auth/logout": lambda scope, receive: self._logout(scope),
        }
        routes.update(
            {
                path: lambda scope, receive: self._totp(
                    scope, receive, TOTP_ROUTES[scope["path"]]
                )
                for path in TOTP_ROUTES
            }
        )
        routes.update(
            {
                path: lambda scope, receive: self._ceremony(
                    scope, receive, scope["path"]
                )
                for path in CEREMONY_ROUTES
            }
        )
        routes.update({path: self._account for path in ACCOUNT_ROUTES})
        return routes

    def _published(self, scope: Any) -> Any:
        """The issuer discovery documents, or ``None`` for any other request."""
        if scope.get("method") != "GET" or self.discovery is None:
            return None
        published = {
            "/.well-known/openid-configuration": self.discovery.document,
            "/.well-known/jwks.json": self.discovery.jwks,
        }
        build = published.get(scope.get("path"))
        return None if build is None else JSONResponse(build())

    async def _federated(self, scope: Any, receive: Any) -> Any:
        """The OIDC redirect, the OIDC callback and the SAML ACS (R009.2.3, R012.2.3.2).

        They are entered by a provider's redirect or form post, so the exact-origin proof
        of the POST routes does not apply; the host check, the one-use state and the
        signed assertion are the proof. Every refusal is the one 401 of ``__call__``.
        """
        if self.federation is None:
            return None
        from arbora.pin_lag.medulla import PinLagUnavailable

        from . import federated as fed

        route = (scope.get("method"), scope.get("path"))
        handlers = {
            ("GET", fed.OIDC_LOGIN): self._oidc_login,
            ("GET", fed.OIDC_CALLBACK): self._oidc_callback,
            ("POST", fed.SAML_ACS): self._saml_acs,
        }
        handler = handlers.get(route)
        if handler is None:
            return None
        self._require_host(scope)
        try:
            return await handler(scope, receive)
        except PinLagUnavailable as missing:
            return JSONResponse(
                {"error": "UNAVAILABLE", "missing": missing.missing}, status_code=503
            )

    def _require_host(self, scope: Any) -> None:
        from urllib.parse import urlsplit

        from .browser import _headers

        if scope.get("scheme") != "https" or _headers(scope, b"host") != (
            urlsplit(self.origin).netloc.encode("ascii"),
        ):
            raise PermissionError("secure browser host required")

    async def _oidc_login(self, scope: Any, receive: Any) -> Any:
        query = _query_fields(scope)
        url = await self.federation.oidc_start(query.get("provider"))
        return RedirectResponse(url, status_code=303)

    async def _oidc_callback(self, scope: Any, receive: Any) -> Any:
        identity = await self.federation.oidc_complete(_query_fields(scope))
        return await self._external(scope, identity)

    async def _saml_acs(self, scope: Any, receive: Any) -> Any:
        identity = await self.federation.saml_complete(
            await _form_fields(scope, receive)
        )
        return await self._external(scope, identity)

    async def _external(self, scope: Any, identity: Any) -> Any:
        """Open the engine session of a verified external identity (``external_login``)."""
        import secrets

        from .approval_engine import identity_value

        credential = secrets.token_urlsafe(32)
        request: dict[str, Any] = {
            "idp_id": identity.idp_id,
            "subject": identity.subject,
            "claims": identity.claims,
            "session_token": credential,
            "ip_prefix": _peer_prefix(scope),
        }
        if identity.username_hint:
            request["username_hint"] = identity.username_hint
        try:
            result = await identity_value(
                self.client,
                self.graph,
                {"family": "credential", "op": "external_login", "request": request},
                "authenticate",
            )
            outcome = result.get("outcome") if isinstance(result, dict) else None
            self.federation.audit(identity.protocol, identity.idp_id, str(outcome))
            if outcome in ("mfa_required", "mfa_enrollment_required"):
                return self._interim(credential, outcome)
            if outcome != "ok":
                raise PermissionError("external sign-in refused")
            response = await self._complete(credential, landing=self.origin + "/")
            if identity.hint:
                response.headers.append("Set-Cookie", _hint_cookie(identity.hint))
            return response
        except BaseException:
            await self._revoke(credential)
            raise

    async def _login_route(self, scope: Any, receive: Any) -> Any:
        return await self._login(scope, await _login_body(scope, receive))

    async def _account(self, scope: Any, receive: Any) -> Any:
        """Register, password change, reset request and reset confirm (R005.2)."""
        from .account_ceremonies import CHANGE_ROUTE, REGISTER, RESET_REQUEST

        path = scope["path"]
        body = await self.accounts.body(scope, receive, path)
        if isinstance(body, JSONResponse):
            return body
        if path == CHANGE_ROUTE:
            return await self._password_change(scope, body)
        if path == REGISTER:
            return await self.accounts.register(body)
        if path == RESET_REQUEST:
            return await self.accounts.reset_request(body)
        return await self.accounts.reset_confirm(body)

    async def _password_change(self, scope: Any, body: dict[str, str]) -> Any:
        """The signed-in person's own change; the other sessions end at the engine."""
        from .browser import require_mutation_proof, session_from_scope

        require_mutation_proof(scope, trusted_origin=self.origin)
        credential = session_from_scope(scope)
        assert credential is not None  # require_mutation_proof refused its absence
        session = await self.caller(await self.issuer.issue(credential), credential)
        return await self.accounts.change(session, body)

    async def _totp(self, scope: Any, receive: Any, operation: str) -> Any:
        """A TOTP step for the signed-in person: the engine holds the secret and checks."""
        from .browser import require_mutation_proof, session_from_scope
        from .mfa_totp import MfaBodyInvalid, totp_body, totp_pass_through

        require_mutation_proof(scope, trusted_origin=self.origin)
        credential = session_from_scope(scope)
        assert credential is not None  # require_mutation_proof refused its absence
        session = await self.caller(await self.issuer.issue(credential), credential)
        try:
            code = await totp_body(scope, receive, operation)
        except MfaBodyInvalid:
            return JSONResponse({"error": "MFA_CODE_INVALID"}, status_code=400)
        return await totp_pass_through(
            self.client,
            self.graph,
            operation,
            tenant=session.tenant,
            session_token=credential,
            code=code,
        )

    async def _ceremony(self, scope: Any, receive: Any, path: str) -> Any:
        """A second-factor step of a pending login (ARBORA-IDENTITY-R006.2.2, .2.3, .4).

        The interim session cookie and the mutation proof are required; the pending
        session is never resolved to a caller here, so it grants nothing. A passed
        ceremony completes the login exactly as a password login does.
        """
        from .browser import require_mutation_proof, session_from_scope
        from .mfa_ceremonies import (
            BEGIN,
            FINISH,
            FINISH_FIELDS,
            MfaBodyInvalid,
            ceremony_body,
        )

        require_mutation_proof(scope, trusted_origin=self.origin)
        credential = session_from_scope(scope)
        assert credential is not None  # require_mutation_proof refused its absence
        fields = (
            frozenset()
            if path == BEGIN
            else FINISH_FIELDS
            if path == FINISH
            else frozenset({"code"})
        )
        try:
            body = await ceremony_body(
                scope, receive, fields, limit=8192 if path == FINISH else 1024
            )
        except MfaBodyInvalid:
            return JSONResponse({"error": "MFA_CODE_INVALID"}, status_code=400)
        if path == BEGIN:
            return await self.ceremonies.begin(credential)
        refusal = await (
            self.ceremonies.finish(credential, body)
            if path == FINISH
            else self.ceremonies.recovery(credential, body["code"])
        )
        return refusal if refusal is not None else await self._complete(credential)

    async def _complete(self, credential: str, landing: str | None = None) -> Any:
        """Issue the full session response for a credential whose login is complete.

        A federated sign-in passes ``landing`` and answers a redirect, not JSON.
        """
        from .browser import session_cookie_header

        token = await self.issuer.issue(credential)
        await self.caller(token, credential)
        state = await self.authority.resolve_session(credential)
        if not isinstance(state, SessionState) or state.resolution.kind != "human":
            raise PermissionError("human browser session required")
        response = _session_response(credential, landing)
        response.headers["Set-Cookie"] = session_cookie_header(
            credential,
            expires_at_ms=state.expires_at_ms,
            now_ms=int(time.time() * 1000),
        ).decode("ascii")
        response._created_session = credential
        return response

    def _interim(self, credential: str, outcome: str) -> Any:
        """The response of a login that waits for a second factor (ARBORA-IDENTITY-R006.2.3).

        The cookie names the engine's pending session and grants nothing: it is not
        resolved to a caller and lives only as long as a challenge.
        """
        from .browser import csrf_token_for, session_cookie_header
        from .mfa_ceremonies import INTERIM_SECONDS

        enrollment = outcome == "mfa_enrollment_required"
        body: dict[str, Any] = {
            "outcome": outcome,
            "csrf_token": csrf_token_for(credential),
        }
        if enrollment:
            body["error"] = "MFA_ENROLLMENT_REQUIRED"
        response = _LoginResponse(body, status_code=403 if enrollment else 200)
        now_ms = int(time.time() * 1000)
        response.headers["Set-Cookie"] = session_cookie_header(
            credential,
            expires_at_ms=now_ms + INTERIM_SECONDS * 1000,
            now_ms=now_ms,
        ).decode("ascii")
        response._created_session = credential
        return response

    async def _logout(self, scope: Any) -> Any:

        from .approval_engine import identity_value
        from .browser import require_mutation_proof, session_from_scope

        require_mutation_proof(scope, trusted_origin=self.origin)
        token = session_from_scope(scope)
        # Resolve who is signing out before the engine forgets the session
        # (ARBORA-IDENTITY-R029.5): logout reaches only the caller's own session.
        state = await self.authority.resolve_session(token)
        await identity_value(
            self.client,
            self.graph,
            {
                "family": "session",
                "op": "revoke",
                "request": {"session_token": token},
            },
            "done",
        )
        self._stop_leases(state, token)
        response = _logout_response(await self._end_session_url(scope))
        _clear_cookie(response)
        return response

    async def _end_session_url(self, scope: Any) -> str | None:
        """The provider's logout URL with ``id_token_hint``, from the sign-in hint cookie."""
        if self.federation is None:
            return None
        from .browser import _headers
        from .federated import HINT_COOKIE

        for header in _headers(scope, b"cookie"):
            for part in header.decode("latin-1").split(";"):
                name, _, value = part.strip().partition("=")
                if name == HINT_COOKIE:
                    return await self.federation.end_session_url(value)
        return None

    def _stop_leases(self, state: Any, token: str | None) -> None:
        """Close every live run lease of the signed-out caller and audit it once."""
        from arbora.security.session_revocation import revoke_own_session

        resolution = getattr(state, "resolution", None)
        if resolution is None:
            return
        import hashlib

        principal = str(resolution.principal_id)
        revoke_own_session(
            tenant=str(resolution.request_context["tenant"]),
            caller_principal=principal,
            session_principal=principal,
            session_id=hashlib.sha256(str(token).encode()).hexdigest()[:16],
            emit=self.ceremonies._audit,
        )

    async def _login(self, scope: Any, body: dict[str, str]) -> Any:
        import secrets

        from .approval_engine import identity_value

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
            outcome = result.get("outcome") if isinstance(result, dict) else None
            if outcome in ("mfa_required", "mfa_enrollment_required"):
                return self._interim(credential, outcome)
            if outcome != "ok":
                raise PermissionError("authentication ceremony incomplete")
            return await self._complete(credential)
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
    from .federated import HINT_COOKIE

    response.headers["Set-Cookie"] = (
        f"{SESSION_COOKIE}=; Max-Age=0; Secure; HttpOnly; SameSite=Lax; Path=/"
    )
    response.headers.append(
        "Set-Cookie",
        f"{HINT_COOKIE}=; Max-Age=0; Secure; HttpOnly; SameSite=Lax; Path=/",
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


def _session_response(credential: str, landing: str | None) -> Any:
    """The completed-login answer: a redirect for a federated landing, else JSON."""
    from .browser import csrf_token_for

    if landing:
        return _LoginRedirect(landing, status_code=303)
    return _LoginResponse({"outcome": "ok", "csrf_token": csrf_token_for(credential)})


def _logout_response(end_session: str | None) -> Any:
    """Signed out: the provider's logout URL when one was hinted, else no content."""
    from starlette.responses import Response

    if end_session:
        return JSONResponse({"end_session_url": end_session})
    return Response(status_code=204)


def _hint_cookie(hint: str) -> str:
    """The provider logout hint, held by the browser and never by Arbora."""
    from .federated import HINT_COOKIE

    return (
        f"{HINT_COOKIE}={hint}; Max-Age=28800; Secure; HttpOnly; SameSite=Lax; Path=/"
    )


def _query_fields(scope: Any) -> dict[str, str]:
    """The query's single-valued fields; a repeated field is refused."""
    from urllib.parse import parse_qs

    parsed = parse_qs(bytes(scope.get("query_string") or b"").decode("latin-1"))
    if any(len(values) != 1 for values in parsed.values()):
        raise PermissionError("repeated query field")
    return {name: values[0] for name, values in parsed.items()}


async def _form_fields(scope: Any, receive: Any) -> dict[str, str]:
    """A bounded urlencoded body (an ACS form post) as single-valued fields."""
    from urllib.parse import parse_qs

    from .browser import _headers

    if _headers(scope, b"content-type") != (b"application/x-www-form-urlencoded",):
        raise PermissionError("form post required")
    data = bytearray()
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            raise PermissionError("form post interrupted")
        data.extend(message.get("body", b""))
        if len(data) > 1 << 20:
            raise PermissionError("form post too large")
        if not message.get("more_body"):
            break
    parsed = parse_qs(bytes(data).decode("latin-1"))
    if any(len(values) != 1 for values in parsed.values()):
        raise PermissionError("repeated form field")
    return {name: values[0] for name, values in parsed.items()}
