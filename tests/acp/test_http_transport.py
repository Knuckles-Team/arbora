"""Real HTTP/SSE SDK tests; scripted model and test authorities/JWT issuer."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import acp
import httpx
import pytest
import uvicorn
from acp import schema
from agent_utilities import host_ports
from agent_utilities.runtime import RunPlan, RunRequest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from starlette.applications import Starlette
from starlette.datastructures import Headers

from arbora.acp.adapter import HostedACPAgent
from arbora.acp.http_transport import AcpHttpLimits, register_acp_routes
from arbora.acp.session_policy import AcpSessionPolicy
from arbora.api.contracts.session import GraphSession, use_session
from arbora.security.actor_context import ActorContext

pytestmark = pytest.mark.spec("ARBORA-ACP-R001.9.2")


class _SyntheticIdentity:
    """Test-only already-verified GraphSessions, never a production authenticator."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        user = headers.get("x-test-user")
        if user is None:
            return await self.app(scope, receive, send)
        tenant = headers.get("x-test-tenant", "tenant")
        actor = ActorContext(actor_id=user, tenant_id=tenant, authenticated=True)
        with use_session(
            GraphSession(
                actor=actor,
                tenant=tenant,
                scopes=frozenset(headers.get("x-test-scopes", "kg:write").split()),
            )
        ):
            await self.app(scope, receive, send)


class _Server(uvicorn.Server):
    def __init__(self, config):
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets=None):
        await super().startup(sockets)
        self.ready.set()


@pytest.fixture
async def http_server(tmp_path, request, monkeypatch):
    root = tmp_path
    (root / "sample.txt").write_text("actual temporary file")
    ended = asyncio.Event()

    async def model(messages, info):
        part = messages[-1].parts[-1]
        if part.part_kind == "tool-return":
            yield str(part.content)
        elif part.content == "read":
            yield {
                0: DeltaToolCall(name="read_file", json_args='{"path":"sample.txt"}')
            }
        elif part.content == "wait":
            try:
                yield "waiting"
                await asyncio.Event().wait()
            finally:
                ended.set()
        else:
            yield "hello "
            yield "remote"

    def plan(request):
        value = RunPlan(
            model=FunctionModel(stream_function=model), steps=(request.task,)
        )
        return replace(
            value, harness=replace(value.harness, client_tools=("read_file",))
        )

    host_ports.register_run_decision_provider(plan)
    app = Starlette()
    parameter = getattr(request, "param", None)
    headers = {"x-test-user": "alice"}
    if parameter == "jwt":
        from arbora.security.request_identity import ActorIdentityMiddleware

        app.add_middleware(ActorIdentityMiddleware)
        app.state.test_token = _test_issuer(monkeypatch)
        headers = {"authorization": "Bearer " + app.state.test_token()}
    else:
        app.add_middleware(_SyntheticIdentity)
    app.state.acp_agent_factory = lambda provider, **kwargs: HostedACPAgent(
        provider,
        policy=AcpSessionPolicy(timedelta(minutes=1), timedelta(minutes=5)),
        trusted_roots=frozenset({str(root)}),
        request_factory=lambda principal, session_id, text: RunRequest(
            task=text, context=session_id
        ),
    )
    register_acp_routes(
        app,
        limits=parameter
        if isinstance(parameter, AcpHttpLimits)
        else AcpHttpLimits(connections=2, frame_bytes=8192, messages=32),
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    server = _Server(uvicorn.Config(app, lifespan="on", log_level="critical"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(3):
        await server.ready.wait()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        async with httpx.AsyncClient(
            base_url=base, headers=headers, timeout=3
        ) as client:
            yield client, root, ended, app
    finally:
        await app.state.acp_close_connections()
        server.should_exit = True
        async with asyncio.timeout(3):
            await task
        sock.close()
        host_ports.register_run_decision_provider(None)


class _Client:
    def __init__(self, root):
        self.root = root
        self.updates = []
        self.waiting = asyncio.Event()

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)
        if getattr(getattr(update, "content", None), "text", None) == "waiting":
            self.waiting.set()

    async def read_text_file(self, session_id, path, **kwargs):
        target = Path(path)
        assert target.is_absolute() and target.is_relative_to(self.root)
        return schema.ReadTextFileResponse(content=target.read_text())


class _HttpTransport(asyncio.Transport):
    def __init__(self):
        super().__init__()
        self.data = b""
        self.closed = False
        self.protocol = None

    def write(self, data):
        self.data = data.rstrip(b"\n")

    def close(self):
        if not self.closed:
            self.closed = True
            self.protocol.connection_lost(None)

    def is_closing(self):
        return self.closed


class _HttpProtocol(asyncio.StreamReaderProtocol):
    def __init__(self, http, path, transport):
        super().__init__(asyncio.StreamReader())
        self.http, self.path, self.output = http, path, transport

    async def _drain_helper(self):
        response = await self.http.post(self.path, content=self.output.data)
        assert response.status_code == 202, response.text


def http_writer(http, path):
    transport = _HttpTransport()
    protocol = _HttpProtocol(http, path, transport)
    transport.protocol = protocol
    protocol.connection_made(transport)
    return asyncio.StreamWriter(transport, protocol, None, asyncio.get_running_loop())


@asynccontextmanager
async def connected(http, root):
    created = await http.post("/api/acp/connections")
    assert created.status_code == 201, created.text
    path = "/api/acp/connections/" + created.json()["connectionId"]
    async with http.stream("GET", path) as events:
        assert events.status_code == 200
        reader = asyncio.StreamReader()

        async def receive():
            try:
                async for line in events.aiter_lines():
                    if line.startswith("data: "):
                        reader.feed_data(line[6:].encode() + b"\n")
            finally:
                reader.feed_eof()

        feeding = asyncio.create_task(receive())
        client = _Client(root)
        conn = acp.connect_to_agent(client, http_writer(http, path), reader)
        try:
            initialized = await conn.initialize(
                protocol_version=1,
                client_capabilities=schema.ClientCapabilities(
                    fs=schema.FileSystemCapabilities(read_text_file=True)
                ),
            )
            assert initialized.protocol_version == 1
            yield conn, client, path
        finally:
            await http.delete(path)
            await conn.close()
            await feeding


async def test_real_http_sdk_stream_and_client_file_read(http_server):
    http, root, _, _ = http_server
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        result = await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("read")]
        )
        assert result.stop_reason == "end_turn"
        text = "".join(
            update.content.text
            for update in client.updates
            if update.session_update == "agent_message_chunk"
        )
        assert text == "actual temporary file"
    assert (await http.get(path)).status_code in {404, 410}


async def test_remote_cancel_stops_real_model_then_allows_fresh_turn(http_server):
    http, root, ended, _ = http_server
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        pending = asyncio.create_task(
            conn.prompt(session_id=session.session_id, prompt=[acp.text_block("wait")])
        )
        await client.waiting.wait()
        await conn.cancel(session_id=session.session_id)
        result = await pending
        assert result.stop_reason == "cancelled" and ended.is_set()
        assert (
            await conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("hello")]
            )
        ).stop_reason == "end_turn"


async def test_carrier_refuses_foreign_identity_replay_and_duplicate(http_server):
    http, root, _, _ = http_server
    async with connected(http, root) as (conn, client, path):
        for headers in [
            {"x-test-user": "bob"},
            {"x-test-tenant": "other"},
            {"x-test-scopes": "kg:admin"},
        ]:
            assert (
                await http.post(path, content=b"{}", headers=headers)
            ).status_code == 403
        assert (
            await http.get(path, headers={"last-event-id": "old"})
        ).status_code == 400
        assert (await http.get(path + "?token=not-a-credential")).status_code == 400
        assert (await http.get(path)).status_code == 409
        for frame, reason in [
            (
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {"protocolVersion": 1},
                },
                "duplicate_request",
            ),
            ({"jsonrpc": "2.0", "id": "stale", "result": {}}, "unexpected_response"),
        ]:
            refused = await http.post(path, content=json.dumps(frame))
            assert refused.status_code == 400 and refused.json()["error"] == reason
        assert (await http.post(path, content=b"x" * 8193)).status_code == 400
        assert (await conn.new_session(cwd=str(root))).session_id


async def test_missing_identity_factory_and_connection_bounds(http_server):
    http, root, _, app = http_server
    assert (
        await http.post(
            "/api/acp/connections",
            headers={"x-test-user": "alice", "x-test-scopes": "kg:read"},
        )
    ).status_code == 403
    missing = http.build_request("POST", "/api/acp/connections")
    del missing.headers["x-test-user"]
    assert (await http.send(missing)).status_code == 401
    factory = app.state.acp_agent_factory
    app.state.acp_agent_factory = None
    assert (await http.post("/api/acp/connections")).status_code == 503
    app.state.acp_agent_factory = factory
    paths = []
    try:
        for _ in range(2):
            created = await http.post("/api/acp/connections")
            paths.append("/api/acp/connections/" + created.json()["connectionId"])
        assert (await http.post("/api/acp/connections")).status_code == 429
        assert (await http.post(paths[0], content=b"{}")).status_code == 409
    finally:
        for path in paths:
            await http.delete(path)


async def test_sse_disconnect_cancels_active_model_without_replay(http_server):
    http, root, ended, _ = http_server
    created = await http.post("/api/acp/connections")
    path = "/api/acp/connections/" + created.json()["connectionId"]
    async with http.stream("GET", path) as events:
        lines = events.aiter_lines()

        async def next_message():
            async for line in lines:
                if line.startswith("data: "):
                    return json.loads(line[6:])
            raise AssertionError("unexpected EOF")

        async def post(identifier, method, params):
            response = await http.post(
                path,
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "method": method,
                        "params": params,
                    }
                ),
            )
            assert response.status_code == 202

        await post(1, "initialize", {"protocolVersion": 1})
        assert (await next_message())["id"] == 1
        await post(2, "session/new", {"cwd": str(root), "mcpServers": []})
        session_id = (await next_message())["result"]["sessionId"]
        await post(
            3,
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": "wait"}]},
        )
        update = await next_message()
        assert update["params"]["update"]["content"]["text"] == "waiting"
    async with asyncio.timeout(2):
        await ended.wait()
    assert (await http.get(path)).status_code in {404, 410}


async def test_inbound_message_count_and_malformed_frames_are_bounded(http_server):
    http, root, _, _ = http_server
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        for bad in [
            b"[]",
            b"{}\n{}",
            b"not-json",
            b'{"jsonrpc":"2.0","id":true,"method":"initialize"}',
            '{"jsonrpc":"2.0"}'.encode("utf-16"),
        ]:
            response = await http.post(path, content=bad)
            assert response.status_code == 400
        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "session/cancel",
                "params": {"sessionId": session.session_id},
            }
        )
        for _ in range(30):
            assert (await http.post(path, content=frame)).status_code == 202
        exceeded = await http.post(path, content=frame)
        assert exceeded.status_code == 400
        assert exceeded.json()["error"] == "message_limit"


@pytest.mark.parametrize(
    "http_server", [AcpHttpLimits(lifetime_seconds=0.3, idle_seconds=2)], indirect=True
)
async def test_connection_lifetime_cancels_without_further_messages(http_server):
    http, root, ended, _ = http_server
    async with connected(http, root) as (conn, client, path):
        # The lifetime clock starts at connection creation, so on a slow runner
        # it can expire before the prompt reaches the model.  Either way the
        # caller must see the connection end, bounded; never wait for the
        # model to start, which it may not have.
        async with asyncio.timeout(10):
            with pytest.raises(ConnectionError):
                session = await conn.new_session(cwd=str(root))
                await conn.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("wait")]
                )
        if client.waiting.is_set():
            async with asyncio.timeout(2):
                await ended.wait()
        assert (await http.get(path)).status_code in {404, 410}


@pytest.mark.parametrize(
    "http_server", [AcpHttpLimits(idle_seconds=0.1)], indirect=True
)
async def test_request_body_has_a_reception_deadline(http_server):
    http, _, _, _ = http_server
    reader, writer = await asyncio.open_connection(
        http.base_url.host, http.base_url.port
    )
    try:
        writer.write(
            b"POST /api/acp/connections HTTP/1.1\r\nHost: synthetic\r\nX-Test-User: alice\r\nContent-Length: 1\r\n\r\n"
        )
        await writer.drain()
        async with asyncio.timeout(2):
            headers = await reader.readuntil(b"\r\n\r\n")
        assert b" 408 " in headers
    finally:
        writer.close()
        await writer.wait_closed()


def test_gateway_registration_does_not_require_optional_acp_package():
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from starlette.applications import Starlette; from arbora.acp.http_transport import register_acp_routes; register_acp_routes(Starlette()); print('registered')",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "registered"


def _test_issuer(monkeypatch):
    """Ephemeral test issuer; only its JWKS retrieval is replaced."""
    import time

    from joserfc import jwt
    from joserfc.jwk import RSAKey

    from arbora.core.settings import runtime_config
    from arbora.security import auth

    key = RSAKey.generate_key(2048, parameters={"kid": "test-only"})
    jwks_uri = "https://issuer.invalid/jwks"
    issuer = "https://issuer.invalid"
    audience = "arbora-test"
    policy_version = "test-policy"
    monkeypatch.setenv("AUTH_JWT_JWKS_URI", jwks_uri)
    monkeypatch.setenv("AUTH_JWT_ISSUER", issuer)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", audience)
    monkeypatch.setenv("AUTH_JWT_ALGORITHMS", "RS256")
    monkeypatch.setenv("KG_POLICY_VERSION", policy_version)
    # This fixture is imported by the central-runtime tests alongside other
    # settings-heavy fixtures.  Remove any earlier process overrides for the
    # values this issuer owns so the live environment is authoritative.  The
    # override layer is restored by pytest's monkeypatch fixture after each
    # server fixture; later drift checks can therefore change the environment
    # and exercise the real profile comparison rather than a pinned override.
    for name in (
        "auth_jwt_jwks_uri",
        "auth_jwt_issuer",
        "auth_jwt_audience",
        "kg_policy_version",
    ):
        monkeypatch.delitem(runtime_config._overrides, name, raising=False)

    async def fetch(uri):
        assert uri == "https://issuer.invalid/jwks"
        return {"keys": [key.as_dict(private=False)]}

    monkeypatch.setattr(auth, "_fetch_jwks", fetch)

    def token(**overrides):
        claims = {
            "sub": "alice",
            "tenant_id": "tenant",
            "scope": "kg:write",
            "iss": issuer,
            "aud": audience,
            "exp": int(time.time()) + 120,
        }
        claims.update(overrides)
        return jwt.encode({"alg": "RS256", "kid": "test-only"}, claims, key)

    return token


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_real_jwt_middleware_over_loopback_sdk(http_server):
    import time

    http, root, _, app = http_server
    token = app.state.test_token
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        result = await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("hello")]
        )
        assert result.stop_reason == "end_turn"
        assert (
            "".join(update.content.text for update in client.updates) == "hello remote"
        )
        for bad in ("invalid", token(exp=int(time.time()) - 60), token(aud="foreign")):
            response = await http.post(
                path, content=b"{}", headers={"authorization": "Bearer " + bad}
            )
            assert response.status_code == 401
        for foreign in (
            token(sub="mallory"),
            token(tenant_id="other"),
            token(scope="kg:read"),
        ):
            response = await http.delete(
                path, headers={"authorization": "Bearer " + foreign}
            )
            assert response.status_code == 403
        assert (await conn.new_session(cwd=str(root))).session_id != session.session_id


async def test_registered_lifespan_closes_connections(http_server):
    http, root, _, app = http_server
    async with app.router.lifespan_context(app):
        created = await http.post("/api/acp/connections")
        assert created.status_code == 201
        path = "/api/acp/connections/" + created.json()["connectionId"]
    assert (await http.delete(path)).status_code == 404


async def test_real_http_close_refuses_subsequent_prompt(http_server):
    http, root, _, _ = http_server
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        await conn.close_session(session_id=session.session_id)
        with pytest.raises(acp.RequestError):
            await conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("hello")]
            )


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_renewal_cannot_revive_expired_authority_between_watch_checks(
    http_server, monkeypatch
):
    import time

    from arbora.acp.http_transport import _Connection

    async def paused_watch(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(_Connection, "_watch_authority", paused_watch)
    http, _, _, app = http_server
    expiry = int(time.time()) + 2
    http.headers["authorization"] = "Bearer " + app.state.test_token(exp=expiry)
    created = await http.post("/api/acp/connections")
    assert created.status_code == 201
    path = "/api/acp/connections/" + created.json()["connectionId"]
    await asyncio.sleep(max(0, expiry - time.time()) + 0.1)
    http.headers["authorization"] = "Bearer " + app.state.test_token(exp=expiry + 120)
    assert (await http.post(path, json={})).status_code == 401


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
@pytest.mark.parametrize(
    "change",
    ["groups", "roles", "kind", "policy", "issuer", "audience", "algorithms", "jwks"],
)
async def test_connection_rejects_verified_authorization_profile_drift(
    http_server, monkeypatch, change
):
    http, _, _, app = http_server
    created = await http.post("/api/acp/connections")
    assert created.status_code == 201
    path = "/api/acp/connections/" + created.json()["connectionId"]
    claims = {}
    if change == "groups":
        claims["groups"] = ["different-group"]
    elif change == "roles":
        claims["roles"] = ["different-role"]
    elif change == "kind":
        claims["email"] = "alice@example.invalid"
    elif change == "policy":
        monkeypatch.setenv("KG_POLICY_VERSION", "changed-policy")
    elif change == "issuer":
        monkeypatch.setenv("AUTH_JWT_ISSUER", "https://new-issuer.invalid")
        claims["iss"] = "https://new-issuer.invalid"
    elif change == "audience":
        monkeypatch.setenv("AUTH_JWT_AUDIENCE", "changed-audience")
        claims["aud"] = "changed-audience"
    elif change == "algorithms":
        monkeypatch.setenv("AUTH_JWT_ALGORITHMS", "RS256,RS384")
    else:
        from arbora.security import auth

        original = auth._fetch_jwks

        async def fetch(uri):
            return await original("https://issuer.invalid/jwks")

        monkeypatch.setattr(auth, "_fetch_jwks", fetch)
        monkeypatch.setenv("AUTH_JWT_JWKS_URI", "https://issuer.invalid/rotated-jwks")
    http.headers["authorization"] = "Bearer " + app.state.test_token(**claims)
    assert (await http.post(path, json={})).status_code == 401
