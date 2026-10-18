"""Boot a real ``arbora`` process against a real EG server (the deploy gate).

The unit suite builds the server without running its startup, so it cannot see
a process that refuses to serve. This test starts the ``epistemic-graph-server``
binary from the installed wheel on a temporary store, then the ``arbora``
console entry point as a subprocess configured through a temporary
``ARBORA_CONFIG_DIR``, and drives both served surfaces with a real bearer
credential:

* HTTP: ``POST /api/v1/ops/query.uql`` and one generated ``eg.*`` operation;
* MCP (streamable HTTP): list tools, ``find``, and ``ask`` an ``eg.*`` operation.

The wheel ships the server only for linux x86_64, so the test skips elsewhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from arbora.deployment.config_generator import PROCESS_IDENTITY_ROLES

pytestmark = [
    pytest.mark.real_boot,
    pytest.mark.skipif(
        not (sys.platform == "linux" and platform.machine() in {"x86_64", "AMD64"}),
        reason="the epistemic-graph-server wheel ships linux x86_64 only",
    ),
]

TENANT = "boot"
ISSUER = "http://127.0.0.1/arbora-boot-test"
AUDIENCE = "arbora-boot-test"
POLICY_VERSION = "boot-test-policy"
TENANT_GRAPH = f"tenant__{TENANT}____commons__"
BOOT_TIMEOUT_S = 180.0
CONNECTOR = "boot-echo"
#: The fixture web-source connector, named as the fleet names the primary backend.
WEB_CONNECTOR = "webarchiver-mcp"
PAGE_HTML = (
    b"<html><head><title>Pod disruption budgets</title></head><body>"
    + b"<p>A pod disruption budget limits voluntary evictions during a node drain.</p>"
    * 30
    + b"</body></html>"
)
PROCESS_SUBJECT = "arbora-process"
#: Graph administration plus the exact capabilities Arbora needs to provision
#: its own semantic packs and reach the MCP fleet.
#: research.web is a scoped write; the operator also holds ``ingest:write``.
ROLES = (*PROCESS_IDENTITY_ROLES, "ingest:write")
#: The served-surface caller. The fresh engine admits exactly one identity
#: (the process principal), so the operator presents a separate credential for
#: that same admitted principal.
OPERATOR_SUBJECT = PROCESS_SUBJECT


#: Process plumbing the boot subprocesses may inherit; nothing that configures Arbora.
_INHERITED_ENV = frozenset(
    {
        "PATH",
        "LANG",
        "LANGUAGE",
        "TZ",
        "TERM",
        "TMPDIR",
        "TEMP",
        "TMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "LD_LIBRARY_PATH",
        "SYSTEMROOT",
        "CI",
    }
)
_INHERITED_ENV_PREFIXES = ("LC_", "PYTHON", "UV_", "COVERAGE_")


def _server_binary() -> str:
    from epistemic_graph.server_binary import (
        ServerBinaryNotFoundError,
        resolve_server_binary,
    )

    try:
        return resolve_server_binary()
    except ServerBinaryNotFoundError as exc:
        pytest.skip(str(exc))
        raise AssertionError("unreachable") from exc


def _listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Issuer:
    """A loopback JWKS endpoint and the RSA key that signs test credentials."""

    def __init__(self) -> None:
        from joserfc.jwk import RSAKey

        self._key = RSAKey.generate_key(2048, parameters={"kid": "boot-test"})
        body = json.dumps({"keys": [self._key.as_dict(is_private=False)]}).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.jwks_uri = f"http://127.0.0.1:{self._server.server_address[1]}/jwks"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def token(self, subject: str, *, ttl: int = 3600, email: bool = True) -> str:
        from joserfc import jwt

        now = int(time.time())
        roles = list(ROLES)
        # A Keycloak client-credentials token carries no email: arbora mints it
        # as an automated_service (service) principal.
        identity = {"email": f"{subject}@boot.example"} if email else {}
        return jwt.encode(
            {"alg": "RS256", "typ": "at+jwt", "kid": "boot-test"},
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "sub": subject,
                **identity,
                "tenant_id": TENANT,
                "roles": roles,
                "scope": " ".join(roles),
                "iat": now,
                "nbf": now - 5,
                "exp": now + ttl,
                "jti": uuid.uuid4().hex,
            },
            self._key,
        )

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


async def _admit_process_identity(
    socket_path: Path, secret: str, signer_key: str
) -> None:
    from epistemic_graph import EpistemicGraphClient

    client = await EpistemicGraphClient.connect(
        socket_path=str(socket_path),
        auth_secret=secret,
        graph_name="__commons__",
        verified_context={
            "principal": PROCESS_SUBJECT,
            "tenant": TENANT,
            "audience": AUDIENCE,
            "agent_id": PROCESS_SUBJECT,
            "roles": [],
            "scopes": ["security:bootstrap"],
            "policy_version": POLICY_VERSION,
            "delegation": [],
        },
    )
    try:
        await client.consensus.bootstrap_system_identity(
            agent_id=PROCESS_SUBJECT, signer_id=PROCESS_SUBJECT, signer_key=signer_key
        )
    finally:
        await client.close()


def _page_server() -> http.server.ThreadingHTTPServer:
    """A loopback page server the fixture web connector fetches from."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE_HTML)))
            self.end_headers()
            self.wfile.write(PAGE_HTML)

        def log_message(self, *_args: Any) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _wait(predicate: Any, *, timeout: float, what: str, logs: Any = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    detail = logs() if callable(logs) else ""
    raise AssertionError(f"timed out waiting for {what}\n{detail}")


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@contextlib.contextmanager
def _booted_stack() -> Iterator[dict[str, Any]]:
    root = Path(tempfile.mkdtemp(prefix="gos-boot-", dir="/tmp"))
    issuer = _Issuer()
    processes: list[subprocess.Popen[Any]] = []
    secret = uuid.uuid4().hex
    signer_key = uuid.uuid4().hex
    socket_path = root / "eg.sock"
    store = root / "store"
    store.mkdir()
    eg_log = (root / "eg.log").open("wb")
    gos_log_path = root / "arbora.log"
    gos_log = gos_log_path.open("wb")
    try:
        # Only process plumbing is inherited. Any Arbora setting in the parent
        # environment (for example one an earlier test projected into
        # os.environ) would override this stack's config.json, so none is.
        base_env = {
            key: value
            for key, value in os.environ.items()
            if key in _INHERITED_ENV or key.startswith(_INHERITED_ENV_PREFIXES)
        }
        eg_env = {
            **base_env,
            "GRAPH_SERVICE_AUTH_SECRET": secret,
            "EPISTEMIC_GRAPH_AUDIENCE": AUDIENCE,
            "EPISTEMIC_GRAPH_TENANT": TENANT,
            "EPISTEMIC_GRAPH_POLICY_VERSION": POLICY_VERSION,
            "EPISTEMIC_GRAPH_REQUIRE_OIDC": "false",
            "EPISTEMIC_GRAPH_SECURITY_STATE_DIR": str(root / "security"),
            # Arbora imports its own semantic packs as its process principal.
            "EPISTEMIC_GRAPH_CONNECTOR_PACK_IMPORTERS": "*=principal:sha256:"
            + hashlib.sha256(PROCESS_SUBJECT.encode()).hexdigest(),
            "EPISTEMIC_GRAPH_SIGNER_KEYS_JSON": json.dumps(
                {
                    PROCESS_SUBJECT: {
                        "key": signer_key,
                        "allowed_roles": [],
                        "may_grant_system": True,
                    }
                }
            ),
        }
        processes.append(
            subprocess.Popen(
                [
                    _server_binary(),
                    "--socket-path",
                    str(socket_path),
                    "--persist-dir",
                    str(store),
                ],
                env=eg_env,
                stdout=eg_log,
                stderr=subprocess.STDOUT,
            )
        )

        def eg_up() -> bool:
            if processes[0].poll() is not None:
                raise AssertionError(
                    "epistemic-graph-server exited during startup\n"
                    + (root / "eg.log").read_text(errors="replace")[-4000:]
                )
            return socket_path.exists()

        _wait(
            eg_up,
            timeout=60,
            what="the epistemic-graph socket",
            logs=lambda: (root / "eg.log").read_text(errors="replace"),
        )

        # The operator's one provisioning step on a fresh engine: admit the
        # arbora process principal as the system identity, signing as itself.
        asyncio.run(_admit_process_identity(socket_path, secret, signer_key))

        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "GRAPH_SERVICE_ENDPOINTS": f"unix://{socket_path}",
                    "DEPLOYMENT_PROFILE": "single-node-prod",
                    "KG_AUTH_TOKEN_REF": "env://ARBORA_BOOT_PROCESS_TOKEN",
                    "AUTH_JWT_JWKS_URI": issuer.jwks_uri,
                    "AUTH_JWT_ISSUER": ISSUER,
                    "AUTH_JWT_AUDIENCE": AUDIENCE,
                    "KG_POLICY_VERSION": POLICY_VERSION,
                    "ENABLE_WEB_UI": "false",
                    "MESSAGING_INTAKE_ENABLED": "false",
                    "ACTION_POLICY_PATH": str(config_dir / "action_policy.json"),
                    # The connector fixture listens on loopback.
                    "MCP_HTTP_ALLOWED_PRIVATE_HOSTS": "127.0.0.1",
                }
            )
        )
        # The operator admits the fleet connector's pack import by policy; the
        # shipped default holds an unknown connector's import for approval.
        from arbora.access.action_policy import DEFAULT_POLICY, TIER_AUTO_NOTIFY

        policy = {
            **DEFAULT_POLICY,
            "rules": [
                {
                    "kind": "connector_pack_import",
                    "target": CONNECTOR,
                    "tier": TIER_AUTO_NOTIFY,
                },
                {
                    "kind": "connector_pack_import",
                    "target": WEB_CONNECTOR,
                    "tier": TIER_AUTO_NOTIFY,
                },
                *DEFAULT_POLICY["rules"],
            ],
        }
        policy_path = config_dir / "action_policy.json"
        policy_path.write_text(json.dumps(policy))
        port = _free_port()
        connector_port = _free_port()
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("_boot_connector.py")),
                    str(connector_port),
                ],
                env=base_env,
                stdout=gos_log,
                stderr=subprocess.STDOUT,
            )
        )
        web_port = _free_port()
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("_boot_web_connector.py")),
                    str(web_port),
                    WEB_CONNECTOR,
                ],
                env=base_env,
                stdout=gos_log,
                stderr=subprocess.STDOUT,
            )
        )
        page_server = _page_server()
        connector_url = f"http://127.0.0.1:{connector_port}/mcp"
        (config_dir / "mcp_config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        CONNECTOR: {
                            "url": connector_url,
                            "transport": "streamable-http",
                        },
                        WEB_CONNECTOR: {
                            "url": f"http://127.0.0.1:{web_port}/mcp",
                            "transport": "streamable-http",
                        },
                    }
                }
            )
        )
        gos_env = {
            **base_env,
            "ARBORA_CONFIG_DIR": str(config_dir),
            "GRAPH_SERVICE_AUTH_SECRET": secret,
            "ARBORA_BOOT_PROCESS_TOKEN": issuer.token(PROCESS_SUBJECT, ttl=7200),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "HOME": str(root),
        }
        bindir = str(Path(sys.executable).parent)
        served_url = f"http://127.0.0.1:{port}/mcp"
        # The documented operator step on a fresh store: provision Arbora's
        # semantic packs under its own process identity before first serve.
        provision = subprocess.run(
            [
                shutil.which("arbora-production-ops", path=bindir)
                or "arbora-production-ops",
                "provision-semantic-content",
                "--served-url",
                served_url,
            ],
            env=gos_env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert provision.returncode == 0, (
            f"semantic provisioning failed\n{provision.stdout}\n{provision.stderr}"
        )
        _wait(
            lambda: _listening(web_port),
            timeout=60,
            what="the web connector fixture",
            logs=lambda: gos_log_path.read_text(errors="replace"),
        )
        _wait(
            lambda: _listening(connector_port),
            timeout=60,
            what="the connector fixture",
            logs=lambda: gos_log_path.read_text(errors="replace"),
        )
        onboard = subprocess.run(
            [
                shutil.which("arbora-production-ops", path=bindir)
                or "arbora-production-ops",
                "onboard-fleet",
                "--served-url",
                served_url,
            ],
            env=gos_env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert onboard.returncode == 0, (
            f"fleet onboarding failed\n{onboard.stdout}\n{onboard.stderr}"
        )
        entry = shutil.which("arbora", path=bindir)
        command = [entry] if entry else [sys.executable, "-m", "arbora.mcp_server"]
        processes.append(
            subprocess.Popen(
                [
                    *command,
                    "--transport",
                    "streamable-http",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--auth-type",
                    "jwt",
                    "--token-jwks-uri",
                    issuer.jwks_uri,
                    "--token-issuer",
                    ISSUER,
                    "--token-audience",
                    AUDIENCE,
                ],
                env=gos_env,
                stdout=gos_log,
                stderr=subprocess.STDOUT,
            )
        )
        base_url = f"http://127.0.0.1:{port}"

        def gos_logs() -> str:
            engine = (root / "eg.log").read_text(errors="replace")
            engine_errors = "\n".join(
                line
                for line in engine.splitlines()
                if "ERROR" in line or "WARN" in line
            )
            return (
                gos_log_path.read_text(errors="replace")[-20000:]
                + "\n--- engine warnings ---\n"
                + engine_errors[-8000:]
            )

        def ready() -> bool:
            import httpx

            if processes[-1].poll() is not None:
                raise AssertionError(f"arbora exited during startup\n{gos_logs()}")
            try:
                return httpx.get(f"{base_url}/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        _wait(ready, timeout=BOOT_TIMEOUT_S, what="arbora readiness", logs=gos_logs)
        yield {
            "socket_path": str(socket_path),
            "secret": secret,
            "base_url": base_url,
            "token": issuer.token(OPERATOR_SUBJECT),
            "service_token": issuer.token(OPERATOR_SUBJECT, email=False),
            "logs": gos_logs,
            "page_url": f"http://127.0.0.1:{page_server.server_address[1]}/pdb",
        }
    finally:
        with contextlib.suppress(NameError):
            page_server.shutdown()
        for process in reversed(processes):
            _terminate(process)
        eg_log.close()
        gos_log.close()
        issuer.close()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="module")
def stack() -> Iterator[dict[str, Any]]:
    with _booted_stack() as booted:
        yield booted


def _post_op(stack: dict[str, Any], op_id: str, params: dict[str, Any]) -> Any:
    import httpx

    response = httpx.post(
        f"{stack['base_url']}/api/v1/ops/{op_id}",
        json=params,
        headers={"Authorization": f"Bearer {stack['token']}"},
        timeout=30,
    )
    assert response.status_code == 200, (
        f"{op_id} answered {response.status_code}: {response.text}\n{stack['logs']()}"
    )
    return response.json()


def test_readiness_passes_against_the_real_engine(stack: dict[str, Any]) -> None:
    """/health/ready reaches 200: the engine probe is valid engine Cypher and
    the authorities this release does not serve do not gate it."""
    import httpx

    deadline = time.monotonic() + 90
    body: Any = None
    while time.monotonic() < deadline:
        response = httpx.get(f"{stack['base_url']}/health/ready", timeout=10)
        body = response.json()
        if response.status_code == 200:
            break
        time.sleep(2)
    assert body["status"] == "ready", f"{body}\n{stack['logs']()}"


def test_a_service_principal_token_is_a_verified_caller(
    stack: dict[str, Any],
) -> None:
    """A no-email (client-credentials) token is a service caller, not refused."""
    import httpx

    response = httpx.post(
        f"{stack['base_url']}/api/v1/ops/query.uql",
        json={"text": "MATCH (:BootProbe) |> LIMIT 1"},
        headers={"Authorization": f"Bearer {stack['service_token']}"},
        timeout=30,
    )
    assert response.status_code == 200, f"{response.text}\n{stack['logs']()}"
    assert response.json()["ok"] is True


@pytest.mark.spec("ARBORA-HOST-R024")
def test_http_serves_uql_and_an_eg_operation(stack: dict[str, Any]) -> None:
    uql = _post_op(stack, "query.uql", {"text": "MATCH (:BootProbe) |> LIMIT 5"})
    assert uql["ok"] is True, uql

    nodes = _post_op(
        stack, "eg.graph.GetNodesByLabel", {"label": "BootProbe", "limit": 5}
    )
    assert nodes["ok"] is True, nodes


async def _mcp_session(stack: dict[str, Any]) -> Any:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    return Client(
        StreamableHttpTransport(
            f"{stack['base_url']}/mcp",
            headers={"Authorization": f"Bearer {stack['token']}"},
        )
    )


def _structured(result: Any) -> dict[str, Any]:
    data = getattr(result, "structured_content", None)
    if data is None:
        data = json.loads(result.content[0].text)
    return data


@pytest.mark.spec("ARBORA-HOST-R024")
async def test_mcp_lists_finds_and_asks_an_eg_operation(
    stack: dict[str, Any],
) -> None:
    async with await _mcp_session(stack) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {"ask", "find", "act", "manage", "write", "why"} <= names, names

        found = _structured(
            await client.call_tool("find", {"intent": "GetNodesByLabel graph"})
        )
        assert found["ok"] is True, found
        listed = json.dumps(found["result"])
        assert "eg.graph.GetNodesByLabel" in listed, listed[:2000]

        asked = _structured(
            await client.call_tool(
                "ask",
                {
                    "op": "eg.graph.GetNodesByLabel",
                    "params": {"label": "BootProbe", "limit": 5},
                },
            )
        )
        assert asked["ok"] is True, f"{asked}\n{stack['logs']()}"


async def test_mcp_find_returns_the_connector_tool(stack: dict[str, Any]) -> None:
    """An onboarded connector's tool is discoverable through ``find``."""
    async with await _mcp_session(stack) as client:
        found = _structured(
            await client.call_tool("find", {"intent": "echo the given text"})
        )
        assert found["ok"] is True, found
        tools = [
            item
            for item in found["result"]["items"]
            if item.get("kind") == "fleet" and item.get("server") == CONNECTOR
        ]
        assert [item["name"] for item in tools] == ["echo"], (
            f"{found}\n{stack['logs']()}"
        )


async def _effect_items(stack: dict[str, Any]) -> list[dict[str, Any]]:
    """The governed effects EG holds on the control graph, read from EG itself."""
    from epistemic_graph import EpistemicGraphClient

    client = await EpistemicGraphClient.connect(
        socket_path=stack["socket_path"],
        auth_secret=stack["secret"],
        graph_name="__control__",
        verified_context={
            "principal": PROCESS_SUBJECT,
            "tenant": TENANT,
            "audience": AUDIENCE,
            "agent_id": PROCESS_SUBJECT,
            "roles": list(ROLES),
            "scopes": list(ROLES),
            "policy_version": POLICY_VERSION,
            "delegation": [],
        },
    )
    try:
        page = await client.work_items.list(tenant=TENANT, kind="arbora.effect")
    finally:
        await client.close()
    return list(page["items"])


async def test_mcp_act_calls_the_connector_tool(stack: dict[str, Any]) -> None:
    """``act(op='fleet.call')`` reaches the connector through the gateway."""
    async with await _mcp_session(stack) as client:
        called = _structured(
            await client.call_tool(
                "act",
                {
                    "op": "fleet.call",
                    "params": {
                        "server": CONNECTOR,
                        "tool": "echo",
                        "arguments": {"text": "boot"},
                    },
                    "execute": True,
                    "idempotency_key": f"boot-echo-{uuid.uuid4().hex[:8]}",
                },
            )
        )
        assert called["ok"] is True, f"{called}\n{stack['logs']()}"
        assert "echo:boot" in json.dumps(called["result"]), called

    # The effect is recorded in EG: one terminal WorkItem holds its outcome.
    effect = await _effect_items(stack)
    assert [item["status"] for item in effect] == ["succeeded"], effect
    assert "echo:boot" in effect[0]["metadata"]["outcome"], effect


@pytest.mark.spec("ARBORA-OPS-R067.3")
def test_research_web_ingests_through_the_real_engine(stack: dict[str, Any]) -> None:
    """``research.web`` commits a fetched page through SourceIngest.

    The page is fetched by the fixture web-source connector, whose pack the
    fleet onboarding imported with ``Document`` and ``Chunk`` mappings, so the
    real engine resolves ``manifest:webarchiver-mcp#schema_mappings/<type>``.
    """
    body = _post_op(
        stack, "research.web", {"urls": [stack["page_url"]], "max_pages": 1}
    )
    value = body["result"]["value"]
    (document,) = value["documents"]
    assert document["document_id"].startswith("web:")
    assert document["backend"] == "webarchiver"
    assert document["chunks"] >= 1
    citation = document["citations"][0]
    assert citation["url"] == stack["page_url"]
    assert citation["backend"] == "webarchiver"
    assert value["ingest"]["affected"] >= 1 + document["chunks"]
