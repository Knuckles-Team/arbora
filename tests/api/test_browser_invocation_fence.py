"""Actual owner bindings with synthetic authority facts; no credentials minted."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from arbora.api.http.auth import HTTPAuthenticationError
from arbora.api.http.browser_auth import BoundBrowserHTTPAuthenticator
from arbora.api.http.routes import make_endpoint
from tests.identity.test_console_admission import (
    CallerFixture,
    OwnersFixture,
    forwarded_scope,
)
from tests.identity.test_modes_and_browser import ORIGIN


def parts():
    backend = OwnersFixture()
    owner = backend.producer()
    scope = forwarded_scope()
    scope.update(type="http", method="POST", path="/ops/fixture.read", query_string=b"")
    scope["state"] = {"arbora_session_admitted": True}
    scope["headers"].append((b"content-type", b"application/json"))
    session = CallerFixture(backend.resolution)
    auth = BoundBrowserHTTPAuthenticator(owner, console_origin=ORIGIN)
    return backend, owner, scope, session, auth


@pytest.mark.parametrize(
    "transition", ["none", "revoke", "narrow", "rotate", "expire", "mfa-revoke"]
)
@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_body_await_transition_is_fenced_before_existing_invoke(transition):
    async def scenario():
        backend, owner, scope, session, auth = parts()
        calls = []

        async def receive():
            if transition == "revoke":
                backend.error = PermissionError("revoked fixture")
            elif transition == "narrow":
                backend.state = replace(
                    backend.state, resolution=backend.resolution.narrow(())
                )
            elif transition == "rotate":
                backend.state = replace(
                    backend.state, session_ref="changed-fixture-binding"
                )
            elif transition == "expire":
                backend.state = replace(backend.state, expires_at_ms=0)
            elif transition == "mfa-revoke":
                backend.state = replace(backend.state, mfa_at_ms=None)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        request = Request(scope, receive)

        async def invoke(*args, **kwargs):
            from arbora.api.contracts.session import current_session
            from arbora.security.actor_context import current_actor

            assert current_session() is session
            assert current_actor() is session.actor
            calls.append(args)
            return {"ok": True}

        endpoint = make_endpoint(
            SimpleNamespace(id="fixture.read"),
            services=SimpleNamespace(registry=SimpleNamespace(digest="fixture")),
            authenticate=auth.authenticate,
            invoke=invoke,
            response=lambda value, op, ref: JSONResponse(value),
            generic=True,
            operation_authority=auth,
        )
        async with owner._bind_request(
            scope,
            session,
            forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
        ):
            response = await endpoint(request)
        assert response.status_code == (200 if transition == "none" else 401)
        assert len(calls) == (1 if transition == "none" else 0)
        assert auth._active.get() is None
        assert owner._requests == {}

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_exact_caller_task_and_once_only_fence():
    async def scenario():
        backend, owner, scope, session, auth = parts()
        request = Request(scope)
        async with owner._bind_request(
            scope,
            session,
            forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
        ):
            async with auth.operation_scope(request) as caller:
                with pytest.raises(HTTPAuthenticationError):
                    await auth.before_invocation(request, replace(caller))
                with pytest.raises(HTTPAuthenticationError):
                    await asyncio.create_task(auth.before_invocation(request, caller))
                await auth.before_invocation(request, caller)
                with pytest.raises(HTTPAuthenticationError):
                    await auth.before_invocation(request, caller)
            with pytest.raises(HTTPAuthenticationError):
                await auth.before_invocation(request, caller)

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_revocation_during_token_verification_await_refuses_authentication():
    async def scenario():
        backend, owner, scope, session, auth = parts()
        original = owner._verify_token

        async def revoking_verifier(token):
            verified = await original(token)
            await asyncio.sleep(0)
            backend.error = PermissionError("revoked during verifier await")
            return verified

        async with owner._bind_request(
            scope,
            session,
            forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
        ):
            owner._verify_token = revoking_verifier
            with pytest.raises(HTTPAuthenticationError):
                async with auth.operation_scope(Request(scope)):
                    pytest.fail("revoked request was admitted")
        assert auth._active.get() is None
        assert owner._requests == {}

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_cancellation_cleans_original_task_and_owner_bindings():
    async def scenario():
        backend, owner, scope, session, auth = parts()
        admitted = asyncio.Event()

        async def operation():
            try:
                async with owner._bind_request(
                    scope,
                    session,
                    forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
                ):
                    async with auth.operation_scope(Request(scope)):
                        admitted.set()
                        await asyncio.Event().wait()
            finally:
                assert auth._active.get() is None

        task = asyncio.create_task(operation())
        await admitted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner._requests == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("revoke", [False, True])
@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_registry_visibility_await_uses_same_owner_fence(revoke):
    from arbora.api.http.app import create_api_application
    from tests.api._support import identity_read_op

    async def scenario():
        backend, owner, scope, session, auth = parts()
        scope.update(method="GET", path="/registry")
        op = identity_read_op(http=None)

        class Registry:
            digest = "fixture"

            def __iter__(self):
                return iter((op,))

            def find(self, caller, *, surface, policy):
                return (op,) if policy(op, caller) else ()

        async def visibility(op, caller):
            await asyncio.sleep(0)
            if revoke:
                backend.error = PermissionError("fixture revoked during policy")
            return True

        async def invoke(*args, **kwargs):
            pytest.fail("registry must not invoke effects")

        app = create_api_application(
            services=SimpleNamespace(registry=Registry()),
            visibility=visibility,
            authenticator=auth,
            invoke=invoke,
        )
        endpoint = next(
            route.endpoint for route in app.routes if route.path == "/registry"
        )
        async with owner._bind_request(
            scope,
            session,
            forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
        ):
            result = await endpoint(Request(scope))
        assert result.status_code == (401 if revoke else 200)
        assert auth._active.get() is None
        assert owner._requests == {}

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_browser_stream_checkout_is_explicitly_unavailable():
    from arbora.api.http.app import create_api_application

    async def scenario():
        _, _, scope, _, auth = parts()

        class Registry:
            digest = "fixture"

            def __iter__(self):
                return iter(())

        async def forbidden(*args, **kwargs):
            pytest.fail("unqualified streaming must not resolve or invoke")

        app = create_api_application(
            services=SimpleNamespace(registry=Registry()),
            visibility=forbidden,
            authenticator=auth,
            invoke=forbidden,
        )
        endpoint = next(
            route.endpoint for route in app.routes if route.path == "/source/checkout"
        )
        response = await endpoint(Request(scope))
        assert response.status_code == 503

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_invocation_error_is_not_relabelled_as_predispatch_refusal():
    async def scenario():
        _, owner, scope, session, auth = parts()
        calls = []

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def invoke(*args, **kwargs):
            calls.append(args)
            raise HTTPAuthenticationError("effect outcome is not a preflight refusal")

        endpoint = make_endpoint(
            SimpleNamespace(id="fixture.read"),
            services=SimpleNamespace(registry=SimpleNamespace(digest="fixture")),
            authenticate=auth.authenticate,
            invoke=invoke,
            response=lambda value, op, ref: JSONResponse(value),
            generic=True,
            operation_authority=auth,
        )
        async with owner._bind_request(
            scope,
            session,
            forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
        ):
            with pytest.raises(HTTPAuthenticationError):
                await endpoint(Request(scope, receive))
        assert len(calls) == 1
        assert auth._active.get() is None
        assert owner._requests == {}

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_concurrent_requests_keep_distinct_private_caller_lifetimes():
    async def scenario():
        backend, owner, scope_a, session_a, auth = parts()
        scope_b = forwarded_scope()
        scope_b.update(
            type="http", method="POST", path="/ops/fixture.read", query_string=b""
        )
        scope_b["state"] = {"arbora_session_admitted": True}
        session_b = CallerFixture(backend.resolution)
        reached = asyncio.Barrier(2)
        callers = []

        async def run(scope, session):
            request = Request(scope)
            async with owner._bind_request(
                scope,
                session,
                forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
            ):
                async with auth.operation_scope(request) as caller:
                    callers.append(caller)
                    await reached.wait()
                    assert caller.session is session
                    await auth.before_invocation(request, caller)
                assert auth._active.get() is None

        await asyncio.gather(run(scope_a, session_a), run(scope_b, session_b))
        assert callers[0] is not callers[1]
        assert owner._requests == {}
        assert auth._active.get() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("outer_mount", [False, True])
@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_actual_asgi_dispatch_requires_binding_after_mount_routing(outer_mount):
    from fastapi import FastAPI

    from arbora.api.http.app import create_api_application
    from tests.api._support import identity_read_op

    async def scenario():
        _, owner, scope, session, auth = parts()
        op = identity_read_op(op_id="fixture.read", http=None)
        scope.update(
            path="/api/v1/ops/fixture.read" if outer_mount else "/ops/fixture.read",
            root_path="",
            scheme="https",
            server=("console.example.test", 443),
            client=("127.0.0.1", 12345),
            http_version="1.1",
        )
        calls, messages = [], []

        class Registry:
            digest = "fixture"

            def __iter__(self):
                return iter((op,))

        async def visibility(*args):
            return True

        async def invoke(*args, **kwargs):
            calls.append(args)
            return {"ok": True}

        async def receive():
            return {
                "type": "http.request",
                "body": b'{"user_id":"fixture"}',
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        app = create_api_application(
            services=SimpleNamespace(registry=Registry()),
            visibility=visibility,
            authenticator=auth,
            invoke=invoke,
        )
        if outer_mount:
            parent = FastAPI()
            parent.mount("/api/v1", app)
            app = parent
        async with owner._bind_request(
            scope,
            session,
            forwarded_token="fixture-local-token",  # sanitizer:ignore - inert synthetic binding fixture, not a credential
        ):
            await app(scope, receive, send)
        status = next(
            item["status"] for item in messages if item["type"] == "http.response.start"
        )
        assert status == (401 if outer_mount else 200)
        assert len(calls) == (0 if outer_mount else 1)
        assert owner._requests == {}
        assert auth._active.get() is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "transition", ["none", "revoke", "ambiguous", "issuer-unavailable", "effect-error"]
)
@pytest.mark.spec("ARBORA-IDENTITY-R004.5.1")
def test_mounted_browser_composition_uses_real_admission_and_same_owner(transition):
    from fastapi import FastAPI

    from arbora.api.http.browser_app import create_browser_api_application
    from arbora.identity.admission import BrowserAdmission
    from arbora.identity.issuer import LocalIssuer
    from tests.api._support import identity_read_op

    async def scenario():
        backend, owner, scope, session, _ = parts()
        scope.update(
            path="/api/v1/ops/fixture.read",
            root_path="",
            scheme="https",
            server=("console.example.test", 443),
            client=("127.0.0.1", 12345),
            http_version="1.1",
        )
        if transition != "ambiguous":
            scope["headers"] = [
                (k, v) for k, v in scope["headers"] if k != b"authorization"
            ]
        op = identity_read_op(op_id="fixture.read", http=None)
        calls, messages, issues = [], [], []

        class Registry:
            digest = "fixture"

            def __iter__(self):
                return iter((op,))

        class FixtureIssuer(LocalIssuer):
            def __init__(self):
                pass

            async def issue(self, credential):
                issues.append(True)
                if transition == "issuer-unavailable":
                    from arbora.identity.engine import IdentityUnavailable

                    raise IdentityUnavailable("private provider detail")
                return "fixture-local-token"

        async def resolve(token, credential):
            assert token == "fixture-local-token"
            return session

        async def visibility(*args):
            return True

        async def invoke(*args, **kwargs):
            calls.append(args)
            if transition == "effect-error":
                raise PermissionError("unknown effect outcome")
            return {"ok": True}

        async def receive():
            if transition == "revoke":
                backend.error = PermissionError("fixture revoked at body await")
            return {
                "type": "http.request",
                "body": b'{"user_id":"fixture"}',
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        admission = BrowserAdmission(
            FixtureIssuer(), owner, resolve, trusted_origin=ORIGIN
        )
        app = FastAPI()
        app.mount(
            "/api/v1",
            create_browser_api_application(
                admission=admission,
                services=SimpleNamespace(registry=Registry()),
                visibility=visibility,
                invoke=invoke,
            ),
        )
        if transition == "effect-error":
            with pytest.raises(PermissionError, match="unknown effect outcome"):
                await app(scope, receive, send)
        else:
            await app(scope, receive, send)
        statuses = [
            item["status"] for item in messages if item["type"] == "http.response.start"
        ]
        assert statuses == [
            200
            if transition == "none"
            else 500
            if transition == "effect-error"
            else 503
            if transition == "issuer-unavailable"
            else 401
        ]
        assert len(calls) == (1 if transition in {"none", "effect-error"} else 0)
        assert len(issues) == (0 if transition == "ambiguous" else 1)
        assert b"private provider detail" not in b"".join(
            item.get("body", b"") for item in messages
        )
        if calls:
            assert calls[0][2].session is session
        assert owner._requests == {}

    asyncio.run(scenario())
