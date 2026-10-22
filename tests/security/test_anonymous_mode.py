"""Explicit anonymous admission never establishes credential authentication."""

from argparse import Namespace
from dataclasses import replace
from types import SimpleNamespace

import pytest

from arbora.security import anonymous

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.3")


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", None)
    monkeypatch.setenv("ARBORA_AUTH_MODE", "none")
    for key in (
        "AUTH_JWT_JWKS_URI",
        "AUTH_JWT_ISSUER",
        "AUTH_JWT_AUDIENCE",
        "KG_AUTH_TOKEN_REF",
        "KG_IDENTITY_OAUTH2",
        "GRAPH_SERVICE_AUTH_SECRET",
        "EPISTEMIC_GRAPH_SIGNER_KEYS_JSON",
    ):
        monkeypatch.delenv(key, raising=False)
    anonymous.validate_startup(Namespace(host="127.0.0.1", auth_type="none"))


def test_anonymous_provenance_is_not_verified(local_mode):
    from arbora.api.contracts.session import SessionRequiredError
    from arbora.api.invoke.steps import AnonymousCaller, authenticate
    from arbora.api.mcp.caller import caller_from_session

    session = anonymous.session()
    assert session.actor.authenticated is False
    with pytest.raises(SessionRequiredError):
        session.engine_verified_context()
    caller = caller_from_session(session, credential_kind="none")
    assert isinstance(caller, AnonymousCaller)
    assert caller.authenticated is False
    assert authenticate(caller) is None
    assert authenticate(replace(caller, authenticated=True)) is not None


def test_anonymous_cannot_satisfy_human_or_service_rule(local_mode):
    from arbora.api.invoke.steps import principal_rule
    from arbora.api.mcp.caller import caller_from_session
    from arbora.api.registry import PrincipalRule

    caller = caller_from_session(anonymous.session(), credential_kind="none")
    for rule in (
        PrincipalRule.HUMAN,
        PrincipalRule.HUMAN_UNDELEGATED,
        PrincipalRule.SERVICE_ONLY,
    ):
        assert principal_rule(SimpleNamespace(principals=rule), caller) is not None
    assert not any(scope.startswith("approvals:") for scope in caller.effective_scopes)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.com", "localhost"])
def test_nonliteral_or_remote_bind_refused(local_mode, host):
    with pytest.raises(ValueError):
        anonymous.validate_startup(Namespace(host=host, auth_type="none"))


@pytest.mark.parametrize(
    "host",
    [b"evil.example", b"localhost@127.0.0.1", b"127.0.0.1/path", b"127.0.0.1:bad"],
)
def test_rebinding_and_malformed_host_refused(local_mode, host):
    with pytest.raises((PermissionError, ValueError)):
        anonymous.validate_request(
            {"client": ("127.0.0.1", 1000), "headers": [(b"host", host)]}
        )


@pytest.mark.parametrize(
    "extra",
    [
        (b"authorization", b"Bearer x"),
        (b"origin", b"http://localhost"),
        (b"cookie", b"a=b"),
    ],
)
def test_credentials_and_browser_origins_refused(local_mode, extra):
    with pytest.raises(PermissionError):
        anonymous.validate_request(
            {"client": ("127.0.0.1", 1000), "headers": [(b"host", b"localhost"), extra]}
        )


def test_mode_cannot_flip_running_process(local_mode, monkeypatch):
    monkeypatch.setenv("ARBORA_AUTH_MODE", "external")
    assert anonymous.enabled()
    with pytest.raises(ValueError, match="new serving process"):
        anonymous.validate_startup(Namespace(host="127.0.0.1", auth_type="jwt"))


def test_default_does_not_enable_none(monkeypatch):
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", None)
    monkeypatch.delenv("ARBORA_AUTH_MODE", raising=False)
    anonymous.validate_startup(Namespace(host="127.0.0.1", auth_type="none"))
    assert not anonymous.enabled()
    with pytest.raises(PermissionError):
        anonymous.session()


def test_anonymous_context_never_calls_verified_carrier(local_mode):
    class Client:
        authenticated = False

        def use_verified_context(self, context):
            raise AssertionError("must not call verified carrier")

    claims = anonymous.session_context(anonymous.session())
    with anonymous.client_scope(Client(), claims):
        pass
    with pytest.raises(PermissionError):
        anonymous.client_scope(SimpleNamespace(authenticated=True), claims)


@pytest.mark.asyncio
async def test_real_asgi_admission_projects_anonymous_caller(local_mode):
    import httpx
    from fastapi import FastAPI, Request

    from arbora.api.http.auth import (
        AmbientHTTPAuthenticator,
        verified_bearer_session_for_request,
    )
    from arbora.security.request_identity import ActorIdentityMiddleware

    app = FastAPI()
    auth = AmbientHTTPAuthenticator(
        session_for_request=verified_bearer_session_for_request
    )

    @app.get("/probe")
    async def probe(request: Request):
        caller = await auth.authenticate(request)
        return {
            "authenticated": caller.authenticated,
            "principal": caller.principal,
            "kind": caller.principal_kind,
        }

    transport = httpx.ASGITransport(
        app=ActorIdentityMiddleware(app), client=("127.0.0.1", 1234)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:18765"
    ) as client:
        response = await client.get("/probe")
        assert response.status_code == 200
        assert response.json() == {
            "authenticated": False,
            "principal": "anonymous",
            "kind": "anonymous",
        }
        assert response.headers["x-arbora-auth-mode"] == "none"
        assert (
            await client.get("/probe", headers={"Authorization": "Bearer forged"})
        ).status_code == 403
        assert (
            await client.get("/probe", headers={"Host": "evil.example"})
        ).status_code == 403
        assert (
            await client.get("/probe", headers={"Origin": "http://127.0.0.1"})
        ).status_code == 403


@pytest.mark.asyncio
async def test_pool_captures_transport_mode_and_omits_claims(local_mode):
    from arbora.epistemic import AuthorityError, EpistemicClientPool
    from arbora.mcp_server.served_engine import _session_context

    calls = []

    class Client:
        authenticated = False

        async def close(self):
            pass

        def use_verified_context(self, claims):
            raise AssertionError("signed carrier called")

    async def connect(**kwargs):
        calls.append(kwargs)
        return Client()

    pool = EpistemicClientPool(connect, tcp_addr="127.0.0.1:19100")
    context = _session_context(anonymous.session())
    async with pool.bind(context, "tenant__local"):
        pass
    assert calls == [
        {
            "graph_name": "tenant__local",
            "auth_mode": "none",
            "tcp_addr": "127.0.0.1:19100",
        }
    ]
    with pytest.raises(AuthorityError, match="mode mismatch"):
        async with pool.bind(
            replace(context, auth_mode="authenticated"), "tenant__local"
        ):
            pass
    await pool.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"principal_kind": "human"},
        {"principal_kind": "service"},
        {"tenant": "other"},
        {"principal": "administrator"},
        {"delegated": True},
        {"mfa_at_ms": 1234},
        {"policy_revision": "other"},
    ],
)
def test_malformed_anonymous_variant_refused(local_mode, updates):
    from arbora.api.invoke.steps import authenticate
    from arbora.api.mcp.caller import caller_from_session

    caller = caller_from_session(anonymous.session(), credential_kind="none")
    assert authenticate(replace(caller, **updates)) is not None


def test_effect_journal_records_honest_anonymous_provenance(local_mode):
    from arbora.mcp_server.effect_journal import _request_context

    claims = anonymous.session_context(anonymous.session())
    context = _request_context(
        claims, SimpleNamespace(graph_name="__control__"), "local-key"
    )
    assert context["authentication_method"] == "none"
    assert context["audience"] == "epistemic-graph"
    assert context["tenant_id"] == "local"
    assert context["subject_id"] == "anonymous"


def test_system_session_does_not_reuse_verified_cache(local_mode, monkeypatch):
    from arbora.security import request_identity

    monkeypatch.setattr(request_identity, "_system_write_session", object())
    session = request_identity.system_write_session()
    assert session.actor.authenticated is False
    assert anonymous.admitted_actor(session.actor)


@pytest.mark.parametrize(
    "field",
    ["static_tokens_ref", "token_secret_ref", "token_jwks_uri", "oidc_client_secret"],
)
def test_explicit_transport_credentials_refused(local_mode, field):
    args = Namespace(
        host="127.0.0.1", auth_type="none", **{field: "synthetic-conflict"}
    )
    with pytest.raises(ValueError, match="credentials"):
        anonymous.validate_startup(args)


@pytest.mark.parametrize(
    "updates",
    [
        {"actor_type": "human"},
        {"roles": ("kg:admin",)},
        {"groups": ("administrators",)},
        {"actor_id": "other"},
        {"tenant_id": "other"},
        {"authority_mode": "verified"},
        {"authenticated": True},
        {"credential_expires_at": 1234},
        {"credential_lease": object()},
    ],
)
def test_anonymous_actor_cannot_claim_verified_identity_attributes(local_mode, updates):
    actor = anonymous.session().actor
    assert not anonymous.admitted_actor(replace(actor, **updates))


@pytest.mark.parametrize(
    "updates",
    [
        {"scopes": frozenset({"rbac:approve-elevation"})},
        {"policy_version": "other"},
        {"audience": "other"},
    ],
)
def test_anonymous_session_cannot_gain_approval_or_replace_policy(local_mode, updates):
    with pytest.raises(PermissionError):
        replace(anonymous.session(), **updates)


def test_surface_admission_cannot_promote_anonymous_flag(local_mode):
    from arbora.api.invoke.steps import surface_caller_admitted
    from arbora.api.mcp.caller import caller_from_session

    caller = caller_from_session(anonymous.session(), credential_kind="none")
    assert surface_caller_admitted(caller)
    assert not surface_caller_admitted(replace(caller, authenticated=True))
    assert not surface_caller_admitted(replace(caller, principal_kind="human"))


def test_old_provider_has_explicit_anonymous_upgrade_refusal(local_mode):
    from arbora.epistemic import AnonymousClientUnavailableError, EpistemicClientPool

    async def old_connect(*, graph_name, verified_context):
        raise AssertionError("transport must not be opened")

    with pytest.raises(AnonymousClientUnavailableError, match="upgrade the matching"):
        EpistemicClientPool(old_connect)


def test_old_provider_is_still_accepted_in_authenticated_mode(monkeypatch):
    from arbora.epistemic import EpistemicClientPool

    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "external")

    async def old_connect(*, graph_name, verified_context):
        raise AssertionError("construction must not open transport")

    EpistemicClientPool(old_connect)


@pytest.mark.asyncio
async def test_real_fastmcp_outer_http_boundary_covers_public_routes(local_mode):
    import httpx
    from agent_connector_sdk.mcp.network import build_network_serving_config
    from fastmcp import FastMCP
    from starlette.responses import JSONResponse

    mcp = FastMCP("anonymous boundary proof")

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request):
        return JSONResponse({"ok": True})

    network = build_network_serving_config(
        Namespace(transport="streamable-http", host="127.0.0.1", port=18765)
    )
    assert network is not None
    kwargs = anonymous.network_run_kwargs(network.fastmcp_run_kwargs())
    app = mcp.http_app(path="/mcp", middleware=kwargs["middleware"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18765"
    ) as client:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.headers["x-arbora-auth-mode"] == "none"
        for headers in (
            {"Authorization": "Bearer forged"},
            {"Host": "evil.example"},
            {"Origin": "http://127.0.0.1"},
            {"Cookie": "session=forged"},
        ):
            assert (await client.get("/health", headers=headers)).status_code == 403


def test_work_item_projection_preserves_anonymous_provenance(local_mode):
    from arbora.work_items import _typed_request_context

    session = anonymous.session()
    context = _typed_request_context(
        anonymous.session_context(session), session, "proof"
    )
    assert context["authentication_method"] == "none"
    assert context["subject_id"] == context["agent_id"] == "anonymous"
    assert context["tenant_id"] == "local"


@pytest.mark.asyncio
async def test_anonymous_policy_preserves_authority_and_narrowing(
    local_mode, monkeypatch
):
    from eunomia_core import schemas

    from arbora.api.mcp.caller import caller_from_session
    from arbora.api.policy.eunomia import PolicyGate, PolicyItem

    caller = caller_from_session(anonymous.session(), credential_kind="none")
    ordinary = PolicyItem("op:query.uql", frozenset({"query:unified"}))
    gate = PolicyGate("none")
    assert await gate.decisions([ordinary], caller, "call") == [True]
    for rule in ("human", "human_undelegated", "service_only"):
        assert await gate.decisions(
            [replace(ordinary, principal_rule=rule)], caller, "call"
        ) == [False]
    assert await gate.decisions(
        [replace(ordinary, required_scopes=frozenset({"unknown:scope"}))],
        caller,
        "call",
    ) == [False]
    for malformed in (
        replace(caller, principal_kind="human"),
        replace(caller, authenticated=True),
        SimpleNamespace(authenticated=True, principal_kind="anonymous"),
        SimpleNamespace(authenticated=False, principal_kind="anonymous"),
        {"authenticated": True, "principal_kind": "anonymous"},
    ):
        assert await gate.decisions([ordinary], malformed, "call") == [False]

    class Deny:
        async def bulk_check(self, requests):
            assert requests[0].principal.attributes["kind"] == "anonymous"
            assert requests[0].principal.attributes["auth_mode"] == "none"
            assert requests[0].principal.attributes["authenticated"] is False
            return [schemas.CheckResponse(allowed=False, reason="fixture")]

    assert await PolicyGate("embedded", Deny()).decisions(
        [ordinary], caller, "call"
    ) == [False]

    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "external")
    assert await gate.decisions([ordinary], caller, "call") == [False]


@pytest.mark.parametrize(
    "name", ["GRAPH_SERVICE_AUTH_SECRET", "EPISTEMIC_GRAPH_SIGNER_KEYS_JSON"]
)
@pytest.mark.parametrize("value", ["", "configured-test-placeholder"])
def test_engine_credentials_checked_through_settings(
    local_mode, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    args = Namespace(host="127.0.0.1", auth_type="none")
    if value:
        with pytest.raises(ValueError, match="must not configure engine secrets"):
            anonymous.validate_startup(args)
    else:
        anonymous.validate_startup(args)


@pytest.mark.parametrize("peer", [None, (), ("192.0.2.1", 1000), ("localhost", 1000)])
def test_anonymous_requires_literal_loopback_peer(local_mode, peer):
    with pytest.raises(PermissionError, match="loopback peer"):
        anonymous.validate_request(
            {"client": peer, "headers": [(b"host", b"localhost")]}
        )


@pytest.mark.parametrize("hosts", [[], [b"localhost", b"127.0.0.1"]])
def test_anonymous_requires_exactly_one_host(local_mode, hosts):
    with pytest.raises(PermissionError, match="One loopback Host"):
        anonymous.validate_request(
            {"client": ("::1", 1000), "headers": [(b"host", value) for value in hosts]}
        )


@pytest.mark.parametrize("host", [b"localhost:80", b"127.0.0.1:80", b"[::1]:80"])
def test_anonymous_accepts_local_host_authorities(local_mode, host):
    anonymous.validate_request({"client": ("::1", 1000), "headers": [(b"host", host)]})


@pytest.mark.parametrize(
    "endpoint", ["tcp://192.0.2.1:80", "example.com:80", "localhost:80"]
)
def test_anonymous_rejects_remote_engine_endpoints(local_mode, monkeypatch, endpoint):
    monkeypatch.setenv("GRAPH_SERVICE_ENDPOINTS", endpoint)
    with pytest.raises(ValueError, match="local engine endpoint"):
        anonymous.validate_startup(Namespace(host="127.0.0.1", auth_type="none"))


@pytest.mark.parametrize(
    "endpoint",
    ["unix:///tmp/engine.sock", "/tmp/engine.sock", "127.0.0.1:80", "tcp://[::1]:80"],
)
def test_anonymous_accepts_local_engine_endpoints(local_mode, monkeypatch, endpoint):
    monkeypatch.setenv("GRAPH_SERVICE_ENDPOINTS", endpoint)
    anonymous.validate_startup(Namespace(host="127.0.0.1", auth_type="none"))
