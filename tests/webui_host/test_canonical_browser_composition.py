"""Actual WebUI factory with synthetic issuer facts; no live auth deployment."""

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from arbora.api.http.browser_app import create_browser_api_application
from arbora.identity.admission import BrowserAdmission
from arbora.identity.issuer import LocalIssuer
from arbora.webui_host.session_boundary import (
    WebUISessionBoundary,
    identity_factory_kwargs,
)
from tests.api._support import identity_read_op
from tests.identity.test_console_admission import CallerFixture, OwnersFixture
from tests.identity.test_modes_and_browser import ORIGIN, request_scope


@pytest.mark.parametrize(
    "transition",
    [
        "none",
        "revoke",
        "missing-cookie",
        "cross-origin",
        "legacy",
        "prefix-confusion",
        "csrf-missing",
        "legacy-outside-api",
        "auth-route",
        "health",
        "pipeline-read",
        "pipeline-invalid",
        "wrong-host",
        "chunked-oversize",
        "false-length",
        "declared-oversize",
        "ambiguous-framing",
        "http-private",
        "validation-private",
        "rate",
        "remote-default-rate",
        "capacity",
        "cancel-capacity",
    ],
)
@pytest.mark.parametrize("auth_mode", ["required", "eg"])
@pytest.mark.spec("ARBORA-IDENTITY-R004.5.2")
def test_actual_webui_factory_dispatches_only_canonical_bound_operations(
    monkeypatch, transition, auth_mode
):
    from agent_webui import oidc_session
    from agent_webui.host import config
    from agent_webui.server import create_agent_web_app
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    for field in ("auth_jwt_jwks_uri", "auth_jwt_issuer", "auth_jwt_audience"):
        monkeypatch.setattr(config, field, None, raising=False)
    monkeypatch.setattr(config, "allowed_hosts", "console.invalid", raising=False)
    monkeypatch.setattr(oidc_session, "load_settings", lambda: None)
    monkeypatch.setenv("KG_AUTH_REQUIRED", "1")
    monkeypatch.setenv("KG_BRAIN_ENFORCE", "1")
    monkeypatch.setenv("AGENT_WEBUI_ACCESS_LOG_POLICY", "disabled")
    monkeypatch.setattr(
        config, "gateway_rate_limit", 0.001 if transition == "rate" else 0
    )
    monkeypatch.setattr(config, "gateway_rate_burst", 1 if transition == "rate" else 0)
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(
        runtime_config, "gateway_rate_limit", 0.001 if transition == "rate" else 0
    )
    monkeypatch.setattr(runtime_config, "gateway_rate_burst", 1)

    async def scenario():
        backend = OwnersFixture()
        owner = backend.producer()
        session = CallerFixture(backend.resolution)
        calls, issued = [], []
        started, release = asyncio.Event(), asyncio.Event()
        op = identity_read_op(op_id="fixture.read", http=None)
        pipeline_services = pipeline_runtime = None
        if transition.startswith("pipeline-"):
            from arbora.api.registry import Effect, Idempotency, Verb
            from tests.api.test_invoke_contract import operation, setup_services

            op = operation(
                id="fixture.read",
                effect=Effect.READ,
                verb=Verb.FIND,
                idempotency=Idempotency.NONE,
                scopes=frozenset(),
            )
            pipeline_services, pipeline_runtime, _, _, _ = setup_services(op)

        class Registry:
            digest = "fixture"

            def __iter__(self):
                return iter((op,))

        class FixtureIssuer(LocalIssuer):
            def __init__(self):
                pass

            async def issue(self, credential):
                issued.append(True)
                return "fixture-local-token"

        async def resolve(token):
            assert token == "fixture-local-token"
            return session

        async def visibility(*args):
            return True

        async def invoke(*args, **kwargs):
            from agent_webui.host import current_grant

            assert current_grant() is None
            calls.append(args)
            if transition in {"capacity", "cancel-capacity"} and len(calls) == 1:
                started.set()
                await release.wait()
            if transition == "http-private":
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=502,
                    detail="PRIVATE_PROVIDER_DETAIL",
                    headers={"X-Secret": "PRIVATE_HEADER"},
                )
            if transition == "validation-private":
                from fastapi.exceptions import RequestValidationError

                raise RequestValidationError(
                    [
                        {
                            "type": "value_error",
                            "loc": ("body", "secret"),
                            "input": "PRIVATE_INPUT",
                            "msg": "PRIVATE_SCHEMA",
                        }
                    ]
                )
            if pipeline_services is not None:
                from arbora.api.invoke import invoke as governed_invoke

                return await governed_invoke(
                    *args, **{**kwargs, "services": pipeline_services}
                )
            return {"ok": True}

        async def auth_app(scope, receive, send):
            from fastapi.responses import Response

            assert transition == "auth-route"
            await Response(status_code=204)(scope, receive, send)

        admission = BrowserAdmission(
            FixtureIssuer(), owner, resolve, trusted_origin=ORIGIN
        )
        operations = create_browser_api_application(
            admission=admission,
            services=SimpleNamespace(registry=Registry()),
            visibility=visibility,
            invoke=invoke,
            max_in_flight=1,
        )
        boundary = WebUISessionBoundary(admission, auth_app, operations=operations)
        if (
            auth_mode == "eg"
            and "auth_mode" not in inspect.signature(create_agent_web_app).parameters
        ):
            from arbora.identity.engine import IdentityUnavailable

            with pytest.raises(IdentityUnavailable, match="lacks explicit"):
                identity_factory_kwargs(
                    auth_mode, boundary, factory=create_agent_web_app
                )
            assert issued == [] and calls == []
            return

        app = create_agent_web_app(
            Agent(TestModel()),
            {"get_path": lambda value: value},
            listener_host="0.0.0.0"
            if transition == "remote-default-rate"
            else "127.0.0.1",
            **identity_factory_kwargs(
                auth_mode, boundary, factory=create_agent_web_app
            ),
        )
        scope = request_scope()
        path = "/api/v1/ops/fixture.read"
        if transition == "legacy":
            path = "/api/enhanced/files/private.md"
        elif transition == "prefix-confusion":
            path = "/api/v1evil/ops/fixture.read"
        elif transition == "legacy-outside-api":
            path = "/run"
        elif transition == "auth-route":
            path = "/auth/login"
        elif transition == "health":
            path = "/health"
        scope.update(
            path=path,
            raw_path=path.encode(),
            root_path="",
            scheme="https",
            http_version="1.1",
            server=("console.invalid", 443),
            client=("127.0.0.1", 12345),
        )
        if transition == "health":
            scope["method"] = "GET"
        scope["headers"].append((b"content-type", b"application/json"))
        if transition == "missing-cookie":
            scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"cookie"]
        elif transition == "wrong-host":
            scope["headers"] = [
                (k, b"untrusted.invalid" if k == b"host" else v)
                for k, v in scope["headers"]
            ]
        elif transition == "csrf-missing":
            scope["headers"] = [
                (k, v) for k, v in scope["headers"] if k != b"x-csrf-token"
            ]
        elif transition == "cross-origin":
            scope["headers"] = [
                (k, b"https://other.invalid" if k == b"origin" else v)
                for k, v in scope["headers"]
            ]
        if transition == "false-length":
            scope["headers"].append((b"content-length", b"1"))
        if transition == "declared-oversize":
            scope["headers"].append((b"content-length", b"2000000"))
        elif transition == "ambiguous-framing":
            scope["headers"].extend(
                [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")]
            )
        messages = []
        receives = 0

        async def receive():
            nonlocal receives
            receives += 1
            if transition in {"chunked-oversize", "false-length"}:
                assert receives <= 2, "must stop reading immediately at byte cap"
                return {
                    "type": "http.request",
                    "body": b"x" * 600_000,
                    "more_body": True,
                }
            if transition == "revoke":
                backend.error = PermissionError("fixture revoked during body")
            body = b'{"user_id":"fixture"}'
            if transition == "pipeline-read":
                body = b'{"value":7}'
            elif transition == "pipeline-invalid":
                body = b'{"_actor":"forged"}'
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            messages.append(message)

        retry_scope = {**scope, "headers": list(scope["headers"])}
        if transition in {"capacity", "cancel-capacity"}:
            first = asyncio.create_task(app(scope, receive, send))
            await started.wait()
            if transition == "capacity":
                await app(retry_scope, receive, send)
                release.set()
                await first
                assert [
                    m["status"] for m in messages if m["type"] == "http.response.start"
                ] == [503, 200]
                assert len(calls) == len(issued) == 1
            else:
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                await app(retry_scope, receive, send)
                assert [
                    m["status"] for m in messages if m["type"] == "http.response.start"
                ] == [200]
                assert len(calls) == len(issued) == 2
            assert operations._in_flight == 0
            assert owner._requests == {}
            return
        await app(scope, receive, send)
        if transition == "rate":
            await app(retry_scope, receive, send)
            assert [
                m["status"] for m in messages if m["type"] == "http.response.start"
            ] == [200, 429]
            assert any(
                k == b"retry-after"
                for item in messages
                if item.get("status") == 429
                for k, _ in item["headers"]
            )
            assert len(calls) == 1 and len(issued) == 2
            assert operations._in_flight == 0
            assert owner._requests == {}
            return
        status = next(
            item["status"] for item in messages if item["type"] == "http.response.start"
        )
        assert status == (
            200
            if transition in {"none", "health", "pipeline-read", "remote-default-rate"}
            else 413
            if transition in {"chunked-oversize", "false-length", "declared-oversize"}
            else 502
            if transition == "http-private"
            else 422
            if transition == "validation-private"
            else 400
            if transition in {"pipeline-invalid", "wrong-host", "ambiguous-framing"}
            else 204
            if transition == "auth-route"
            else 503
            if transition in {"legacy", "prefix-confusion", "legacy-outside-api"}
            else 401
        )
        assert len(calls) == (
            1
            if transition
            in {
                "none",
                "pipeline-read",
                "pipeline-invalid",
                "remote-default-rate",
                "http-private",
                "validation-private",
            }
            else 0
        )
        assert len(issued) == (
            1
            if transition
            in {
                "none",
                "revoke",
                "pipeline-read",
                "pipeline-invalid",
                "remote-default-rate",
                "chunked-oversize",
                "false-length",
                "http-private",
                "validation-private",
            }
            else 0
        )
        if calls:
            assert calls[0][2].session is session
        if transition in {"declared-oversize", "ambiguous-framing"}:
            assert receives == 0
        assert "PRIVATE_" not in str(messages)
        if transition in {"chunked-oversize", "false-length"}:
            assert receives == 2
        if pipeline_runtime is not None:
            assert len(pipeline_runtime.calls) == (
                1 if transition == "pipeline-read" else 0
            )
        if transition == "remote-default-rate":
            limiter = operations.authenticator._rate_limiter
            assert limiter.rate == float(config.gateway_rate_limit) > 0
            assert limiter.burst == float(config.gateway_rate_burst) > 0
            assert float(runtime_config.gateway_rate_limit) == 0
        assert owner._requests == {}

    asyncio.run(scenario())
