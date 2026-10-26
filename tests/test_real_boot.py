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
from typing import Any, cast

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
INGEST_CONNECTOR = "boot-ingest"
PROCESS_SUBJECT = "arbora-process"
#: Graph administration plus the exact capabilities Arbora needs to provision
#: its own semantic packs and reach the MCP fleet.
ROLES = PROCESS_IDENTITY_ROLES
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
        probe.settimeout(1)
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

    def token(
        self,
        subject: str,
        *,
        ttl: int = 3600,
        email: bool = True,
        roles: tuple[str, ...] | None = None,
        tenant: str = TENANT,
    ) -> str:
        from joserfc import jwt

        now = int(time.time())
        effective_roles = tuple(ROLES if roles is None else roles)
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
                "tenant_id": tenant,
                "roles": list(effective_roles),
                "scope": " ".join(effective_roles),
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


def _wait(predicate: Any, *, timeout: float, what: str, logs: Any = None) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    detail = logs() if callable(logs) else ""
    raise AssertionError(f"timed out waiting for {what}\n{detail}")


def _wait_for_model(process: subprocess.Popen[Any], port: int, log_path: Path) -> None:
    def ready() -> bool:
        if process.poll() is not None:
            raise AssertionError(
                "model fixture exited during startup\n"
                + log_path.read_text(errors="replace")[-4000:]
            )
        return _listening(port)

    _wait(
        ready,
        timeout=10,
        what="the model fixture",
        logs=lambda: log_path.read_text(errors="replace")[-4000:],
    )


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@contextlib.contextmanager
def _booted_stack(
    extra_config: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    extra_token_roles: tuple[str, ...] = (),
) -> Iterator[dict[str, Any]]:
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

        model_port = _free_port()
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("_boot_model.py")),
                    str(model_port),
                ],
                env=base_env,
                stdout=eg_log,
                stderr=subprocess.STDOUT,
            )
        )
        _wait_for_model(processes[-1], model_port, root / "eg.log")
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
                    # The connector and model fixtures listen on loopback.
                    "MCP_HTTP_ALLOWED_PRIVATE_HOSTS": "127.0.0.1",
                    "MODEL_HTTP_ALLOWED_PRIVATE_HOSTS": "127.0.0.1",
                    "CHAT_MODELS": json.dumps(
                        [
                            {
                                "id": "boot-model",
                                "base_url": f"http://127.0.0.1:{model_port}/v1",
                                "api_key_env": "ARBORA_BOOT_MODEL_KEY",
                            }
                        ]
                    ),
                    **(extra_config or {}),
                }
            )
        )
        # The operator admits the fleet connector's pack import by policy; the
        # shipped default holds an unknown connector's import for approval.
        from arbora.access.action_policy import DEFAULT_POLICY, TIER_AUTO_NOTIFY

        policy = {
            **DEFAULT_POLICY,
            "rules": [
                *(
                    {
                        "kind": "connector_pack_import",
                        "target": target,
                        "tier": TIER_AUTO_NOTIFY,
                    }
                    for target in (CONNECTOR, INGEST_CONNECTOR)
                ),
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
        ingest_port = _free_port()
        processes.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).with_name("_boot_ingest_connector.py")),
                    str(ingest_port),
                ],
                env=base_env,
                stdout=gos_log,
                stderr=subprocess.STDOUT,
            )
        )
        connector_url = f"http://127.0.0.1:{connector_port}/mcp"
        (config_dir / "mcp_config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        CONNECTOR: {
                            "url": connector_url,
                            "transport": "streamable-http",
                        },
                        INGEST_CONNECTOR: {
                            "url": f"http://127.0.0.1:{ingest_port}/mcp",
                            "transport": "streamable-http",
                        },
                    }
                }
            )
        )
        token_roles = tuple(dict.fromkeys((*ROLES, *extra_token_roles)))
        process_token = issuer.token(PROCESS_SUBJECT, ttl=7200)
        service_token = issuer.token(PROCESS_SUBJECT, ttl=7200, email=False)
        gos_env = {
            **base_env,
            "ARBORA_CONFIG_DIR": str(config_dir),
            "GRAPH_SERVICE_AUTH_SECRET": secret,
            "ARBORA_BOOT_PROCESS_TOKEN": process_token,
            "ARBORA_BOOT_MODEL_KEY": "boot-model-key",  # sanitizer:ignore - synthetic
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_STATE_HOME": str(root / "xdg-state"),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "HOME": str(root),
            **(extra_env or {}),
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
            lambda: _listening(connector_port) and _listening(ingest_port),
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

        def restart_engine() -> None:
            """Reopen this fixture's same durable store, without re-enrollment."""
            command = processes[0].args
            _terminate(processes[0])
            processes[0] = subprocess.Popen(
                command, env=eg_env, stdout=eg_log, stderr=subprocess.STDOUT
            )

            def reopened() -> bool:
                if not eg_up():
                    return False
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    try:
                        probe.connect(str(socket_path))
                    except OSError:
                        return False
                    return True

            _wait(
                reopened,
                timeout=60,
                what="the restarted epistemic-graph socket",
                logs=lambda: (root / "eg.log").read_text(errors="replace"),
            )

        yield {
            "restart_engine": restart_engine,
            "socket_path": str(socket_path),
            "secret": secret,
            "base_url": base_url,
            "token": issuer.token(OPERATOR_SUBJECT, roles=token_roles),
            "foreign_token": issuer.token(
                OPERATOR_SUBJECT, roles=token_roles, tenant="foreign-boot"
            ),
            "service_token": service_token,
            "issuer_jwks_uri": issuer.jwks_uri,
            "logs": gos_logs,
        }
    finally:
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


@pytest.mark.spec(
    "ARBORA-OPS-R021.4.5.1.2", "ARBORA-OPS-R021.4.7", "ARBORA-OPS-R021.4.8.1"
)
def test_native_current_plan_lifecycle_uses_installed_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The released EG lifecycle carries one native submit/claim into the
    admitted publication and reads it through the real HTTP/MCP serving root.
    Replayed events below are controlled durable fixtures, not an AU run.

    The extra work scopes belong only to this ephemeral issuer/token. The
    production process role declaration remains the least privilege fixture
    used by the rest of the boot suite.
    """
    from types import SimpleNamespace

    from agent_connector_sdk.current_plan import PlanItem
    from epistemic_graph import EpistemicGraphClient

    from arbora import current_plan_publication, work_items
    from arbora.a2a.authority import WorkItemA2AAuthority
    from arbora.api.contracts.session import use_session
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.invoke.steps import OpResult
    from arbora.api.mcp.caller import caller_from_session
    from arbora.api.registry import Surface
    from arbora.core.settings import runtime_config
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.epistemic import ConnectClient, EpistemicClientPool
    from arbora.mcp_server.current_plan_reader import NativeCurrentPlanReader
    from arbora.mcp_server.served_engine import ServedEngine
    from arbora.mcp_server.served_invocation import (
        compose_served_invocation,
        refused_fleet_effect,
    )
    from arbora.security.request_identity import (
        actor_from_bearer_token,
        mint_graph_session,
    )

    with _booted_stack(
        extra_token_roles=("work:read", "work:write"),
    ) as stack:
        monkeypatch.setattr(
            runtime_config, "auth_jwt_jwks_uri", stack["issuer_jwks_uri"]
        )
        monkeypatch.setattr(runtime_config, "auth_jwt_issuer", ISSUER)
        monkeypatch.setattr(runtime_config, "auth_jwt_audience", AUDIENCE)
        monkeypatch.setattr(runtime_config, "kg_policy_version", POLICY_VERSION)
        actor = asyncio.run(actor_from_bearer_token(stack["token"]))
        session = mint_graph_session(actor).with_graph(CONTROL_GRAPH)
        pool = EpistemicClientPool(
            cast(ConnectClient, EpistemicGraphClient.connect),
            socket_path=stack["socket_path"],
            auth_secret=stack["secret"],
        )
        engine = ServedEngine(pool)
        invocation = compose_served_invocation(
            pool=pool,
            graph=CONTROL_GRAPH,
            process_session=lambda: session,
            bindings={"current_plan_reader": NativeCurrentPlanReader()},
            fleet_effect=refused_fleet_effect,
        )
        run_id = f"native-current-plan-{uuid.uuid4().hex}"

        async def exercise() -> None:
            with use_session(session):
                item_id, created = work_items.submit_work_item_atomic(
                    engine,
                    kind="orchestrator_task",
                    queue="orchestrator_task",
                    resource_class="agent_dispatch",
                    tenant=TENANT,
                    run_id=run_id,
                    idempotency_key=run_id,
                    payload_ref=run_id,
                    description="installed current-plan lifecycle acceptance",
                    created_by=PROCESS_SUBJECT,
                    metadata={
                        "a2a_schema": "arbora-a2a-unary-v1",
                        "a2a_owner_ref": WorkItemA2AAuthority._owner_ref(session),
                        "a2a_run_id": run_id,
                    },
                )
                assert created is True
                claim = work_items.claim_specific(
                    engine,
                    item_id,
                    token=f"native-current-plan-worker:{run_id}",
                    lease_ttl_s=600,
                )
                assert claim is not None
                request = SimpleNamespace(
                    run_id=run_id,
                    metadata={"session": session},
                )
                with current_plan_publication.bind_admitted_claim(
                    engine,
                    session=session,
                    run_id=run_id,
                    work_item_id=item_id,
                    claim=claim,
                    hosted_invocation=invocation,
                    owner_graph=CONTROL_GRAPH,
                ):
                    async with current_plan_publication.publication_scope(
                        request
                    ) as publisher:
                        assert publisher is not None
                        await publisher.publish(0, ())
                        await publisher.publish(
                            1,
                            (PlanItem(text="inspect", status="in_progress"),),
                        )

                caller = caller_from_session(
                    session,
                    credential_kind="session",
                    request_id=f"current-plan-read:{run_id}",
                )
                result = await invoke(
                    "runs.current_plan",
                    {"run_id": run_id},
                    caller,
                    Surface.HTTP,
                    services=invocation.services,
                )
                assert isinstance(result, OpResult), result
                assert result.code == "OK", result
                value = result.value["value"]
                assert value["availability"] == "available"
                assert value["snapshot"]["run_id"] == run_id
                assert value["snapshot"]["items"] == [
                    {"text": "inspect", "status": "in_progress"}
                ]
                # Replay is stored on the caller graph, as in the served run.
                with use_session(session.with_graph(TENANT_GRAPH)):
                    _seed_native_read_events(engine, run_id)
                await _check_served_native_reads(stack, run_id, item_id)
                assert (
                    work_items.commit_result(
                        engine,
                        item_id,
                        claim,
                        outcome="succeeded",
                        result_ref=f"run:{run_id}",
                    )
                    == "committed"
                )
                completed = await _read_native_surfaces(
                    stack, {"runs.get": {"run_id": run_id, "task_id": item_id}}
                )
                assert completed["runs.get"]["status"] == "completed", completed
            await pool.close()

        try:
            asyncio.run(exercise())
        except Exception as exc:
            raise AssertionError(
                "native current-plan lifecycle failed\n" + stack["logs"]()
            ) from exc


def _seed_native_read_events(engine: Any, run_id: str) -> None:
    """Controlled durable rows; no producer or executable-plan claim."""
    for sequence, kind in enumerate(
        ("run_started", "public_summary", "graph_complete"), 1
    ):
        event_id = f"{run_id}:{sequence}"
        event = {
            "run_id": run_id,
            "event_id": event_id,
            "sequence": sequence,
            "type": kind,
            "summary": "Checked the installed read boundary",
            "reasoning": "PRIVATE READ FIXTURE",
        }
        assert engine.create_node(
            f"runevent:{event_id}",
            "RunEvent",
            {
                "run_id": run_id,
                "sequence": sequence,
                "event_id": event_id,
                "event_type": kind,
                "payload": json.dumps(event),
            },
        )


async def _paced_native_read(
    client: Any,
    operation: str,
    params: dict[str, Any],
    *,
    raise_on_error: bool = True,
) -> Any:
    # The installed MCP middleware allows 10 requests/s with a burst of 20,
    # including session handshakes. Keep this read journey at <=5 calls/s
    # without changing the production budget or retrying a refused request.
    await asyncio.sleep(0.2)
    return await client.call_tool(
        "ask", {"op": operation, "params": params}, raise_on_error=raise_on_error
    )


async def _read_native_surfaces(
    stack: dict[str, Any], requests: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Every call enters the installed process; no injected read bindings."""
    values = {}
    async with await _mcp_session(stack) as client:
        for operation, params in requests.items():
            http = await asyncio.to_thread(_post_op, stack, operation, params)
            mcp = _structured(await _paced_native_read(client, operation, params))
            assert http["ok"] is True and mcp["ok"] is True, (http, mcp)
            assert http["result"] == mcp["result"], (http, mcp)
            values[operation] = http["result"]["value"]
    return values


async def _check_served_native_reads(
    stack: dict[str, Any], run_id: str, item_id: str
) -> None:
    pair = {"run_id": run_id, "task_id": item_id}
    requests: dict[str, dict[str, Any]] = {
        "runs.current_plan": {"run_id": run_id},
        "runs.get": pair,
        "runs.events": {**pair, "limit": 2},
        "runs.execution_graph": {**pair, "schema_version": "arbora-execution-graph-v1"},
    }
    first = await _read_native_surfaces(stack, requests)
    assert first["runs.current_plan"]["availability"] == "available", first
    assert first["runs.current_plan"]["snapshot"]["items"] == [
        {"text": "inspect", "status": "in_progress"}
    ]
    assert first["runs.get"]["status"] == "running", first
    assert first["runs.get"]["public_summary"] == {"availability": "unavailable"}
    assert first["runs.get"]["task_plan"] == {"availability": "unavailable"}
    page = first["runs.events"]
    assert page["availability"] == "available" and page["has_more"] is True, page
    assert [event["sequence"] for event in page["events"]] == [1, 2]
    assert page["events"][1]["payload"] == {
        "summary": "Checked the installed read boundary"
    }
    assert "PRIVATE" not in json.dumps(first)
    assert first["runs.execution_graph"]["availability"] == "unavailable"
    # A fresh MCP session must return the identical persisted projection.
    assert await _read_native_surfaces(stack, requests) == first
    resumed = await _read_native_surfaces(
        stack, {"runs.events": {**pair, "after": page["next_after"]}}
    )
    tail = resumed["runs.events"]
    assert [event["sequence"] for event in tail["events"]] == [3]
    assert tail["terminal"] is True and tail["has_more"] is False
    await _check_foreign_native_reads(stack, requests)


def _assert_native_read_refusal(value: dict[str, Any]) -> None:
    """A broken backend or unknown operation is not isolation evidence."""
    if value.get("ok") is True:
        assert value["result"]["value"]["availability"] == "unavailable", value
    else:
        assert value.get("ok") is False, value
        assert value["error"]["code"] in {
            "UNAUTHENTICATED",
            "ACCESS_DENIED",
            "SUBJECT_ACCESS_DENIED",
            "PRINCIPAL_NOT_ALLOWED",
            "SCOPE_REQUIRED",
            "POLICY_DENIED",
        }, value
    _assert_no_native_read_payload(json.dumps(value))


def _assert_no_native_read_payload(encoded: str) -> None:
    assert "Checked the installed read boundary" not in encoded
    assert '"text": "inspect"' not in encoded
    assert "PRIVATE" not in encoded


async def _check_foreign_native_reads(
    stack: dict[str, Any], requests: dict[str, dict[str, Any]]
) -> None:
    import httpx

    foreign = {**stack, "token": stack["foreign_token"]}
    async with httpx.AsyncClient() as http:
        for operation, params in requests.items():
            response = await http.post(
                f"{stack['base_url']}/api/v1/ops/{operation}",
                json=params,
                headers={"Authorization": f"Bearer {foreign['token']}"},
                timeout=30,
            )
            assert response.status_code in (200, 401, 403), response.text
            _assert_no_native_read_payload(response.text)
            value = response.json()
            # HTTP middleware may reject authentication before an op envelope.
            if response.status_code == 200 or "ok" in value:
                _assert_native_read_refusal(value)
    await _check_foreign_native_mcp(foreign, requests)


async def _check_foreign_native_mcp(
    foreign: dict[str, Any], requests: dict[str, dict[str, Any]]
) -> None:
    import httpx2

    async with contextlib.AsyncExitStack() as contexts:
        try:
            client = await contexts.enter_async_context(await _mcp_session(foreign))
        except httpx2.HTTPStatusError as exc:
            # Locked FastMCP 4.1 passes httpx2.HTTPStatusError through its
            # _connection_failure boundary. Only handshake 401/403 qualifies;
            # timeouts, connection failures and server errors must fail this test.
            assert exc.response.status_code in (401, 403), exc.response
            await exc.response.aread()
            _assert_no_native_read_payload(exc.response.text)
            return
        for operation, params in requests.items():
            result = _structured(
                await _paced_native_read(
                    client, operation, params, raise_on_error=False
                )
            )
            _assert_native_read_refusal(result)


def _post_op_keyed(stack: dict[str, Any], op_id: str, params: dict[str, Any]) -> Any:
    import httpx

    response = httpx.post(
        f"{stack['base_url']}/api/v1/ops/{op_id}",
        json=params,
        headers={
            "Authorization": f"Bearer {stack['token']}",
            "Idempotency-Key": f"boot-{uuid.uuid4().hex[:12]}",
        },
        timeout=120,
    )
    assert response.status_code == 200, (
        f"{op_id} answered {response.status_code}: {response.text}\n{stack['logs']()}"
    )
    return response.json()


async def _direct_submit(stack: dict[str, Any], connector: str) -> str:
    """Submit one record straight to the engine; return its refusal (diagnostics)."""
    from agent_connector_sdk.ingest.model import ChangeSet, Entity, IngestBinding
    from agent_connector_sdk.ingest.service import KnowledgeIngest
    from agent_connector_sdk.ingest.transport import EpistemicGraphIngestTransport
    from epistemic_graph import EpistemicGraphClient

    client = await EpistemicGraphClient.connect(
        socket_path=stack["socket_path"],
        auth_secret=stack["secret"],
        graph_name=TENANT_GRAPH,
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
        await KnowledgeIngest(EpistemicGraphIngestTransport(client)).submit(
            IngestBinding(connector=connector, stream="probe"),
            ChangeSet(entities=(Entity(id="probe-1", node_type="FixtureItem"),)),
        )
        return "accepted"
    except Exception as exc:  # diagnostics only
        return f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()


async def _pack_status(stack: dict[str, Any], connector: str) -> Any:
    """The engine's ConnectorPack status for ``connector`` (diagnostics)."""
    from epistemic_graph import EpistemicGraphClient
    from epistemic_graph.generated.connector_pack import ConnectorPackStatusRequest
    from epistemic_graph.generated.storage import send_connector_pack_status

    client = await EpistemicGraphClient.connect(
        socket_path=stack["socket_path"],
        auth_secret=stack["secret"],
        graph_name=TENANT_GRAPH,
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
        return await send_connector_pack_status(
            client,
            ConnectorPackStatusRequest(connector=connector, tenant_id=TENANT),
            TENANT_GRAPH,
        )
    finally:
        await client.close()


@pytest.mark.spec("ARBORA-OPS-R020.6.6")
def test_ingest_sources_list_sync_status(stack: dict[str, Any]) -> None:
    """A declared connector source lists, syncs as a WorkItem, and lands in EG."""
    source_id = f"{INGEST_CONNECTOR}/items"
    listed = _post_op(stack, "ingest.sources.list", {})
    assert listed["ok"] is True, listed
    sources = {s["source_id"]: s for s in listed["result"]["value"]["sources"]}
    assert sources[source_id]["record_type"] == "FixtureItem", listed

    synced = _post_op_keyed(
        stack, "ingest.sources.sync", {"source_id": source_id, "mode": "full"}
    )
    assert synced["ok"] is True, synced
    receipt = synced["result"]["value"]
    status = _post_op(stack, "ingest.sources.status", {"source_id": source_id})
    record = status["result"]["value"]
    if record.get("sync_reason") is not None:
        direct = asyncio.run(_direct_submit(stack, INGEST_CONNECTOR))
        raise AssertionError(f"{record}\nDIRECT SUBMIT: {direct}")
    assert record["last_job_id"] == receipt["job_id"], (status, stack["logs"]())
    assert receipt["status"] == "completed", (receipt, record, stack["logs"]())
    assert record["records"] == 3, record

    # Records land as the declared mapping's ontology class, each pointing
    # back at the connector's FixtureItem mapping.
    nodes = _post_op(
        stack, "eg.graph.GetNodesByLabel", {"label": "Document", "limit": 10}
    )
    assert nodes["ok"] is True, nodes
    mapped = {
        props["source_record_id"]
        for _node_id, props in nodes["result"]
        if props.get("source_mapping_reference")
        == f"connector:{INGEST_CONNECTOR}#mappings/FixtureItem"
    }
    assert mapped == {"item-1", "item-2", "item-3"}, json.dumps(nodes)[:3000]


# ── A2A: the TUI's transport ─────────────────────────────────────────────────


def _a2a(stack: dict[str, Any], method: str, params: dict[str, Any], token: str):
    import httpx

    key = uuid.uuid4().hex
    return httpx.post(
        f"{stack['base_url']}/a2a",
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params},
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        timeout=120,
    )


def _boot_message(text: str) -> dict[str, Any]:
    return {
        "kind": "message",
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": uuid.uuid4().hex,
        "contextId": f"tui-{uuid.uuid4().hex}",
    }


@pytest.mark.spec("ARBORA-OPS-R001")
def test_a2a_answers_a_rejected_bearer_with_401(stack: dict[str, Any]) -> None:
    """0.1.5: an expired or wrongly signed bearer made /a2a and its card 500.

    The TUI then reported an empty "A2A stream failed" instead of the refusal.
    """
    import httpx

    card = httpx.get(
        f"{stack['base_url']}/a2a/.well-known/agent-card.json",
        headers={"Authorization": "Bearer not-a-valid-token"},
        timeout=30,
    )
    assert card.status_code == 401, card.text
    send = _a2a(
        stack, "message/send", {"message": _boot_message("hi")}, "not-a-valid-token"
    )
    assert send.status_code == 401, send.text
    assert send.json()["error"]["message"] == "A2A authority refused"


@pytest.mark.spec("ARBORA-OPS-R001")
def test_message_stream_refuses_as_unsupported(stack: dict[str, Any]) -> None:
    response = _a2a(
        stack, "message/stream", {"message": _boot_message("hi")}, stack["token"]
    )
    assert response.status_code == 501, response.text
    assert response.json()["error"]["code"] == -32010


@pytest.mark.spec("ARBORA-OPS-R001")
def test_a2a_message_send_completes_through_the_model(stack: dict[str, Any]) -> None:
    response = _a2a(
        stack, "message/send", {"message": _boot_message("hello")}, stack["token"]
    )
    assert response.status_code == 200, response.text
    task = response.json()["result"]
    for _ in range(60):
        if task["status"]["state"] in {"completed", "failed"}:
            break
        time.sleep(1)
        task = _a2a(stack, "tasks/get", {"id": task["id"]}, stack["token"]).json()[
            "result"
        ]
    assert task["status"]["state"] == "completed", f"{task}\n{stack['logs']()}"
    assert "example.org/arbora-boot" in json.dumps(task.get("artifacts"))
    assert "not persisted" not in stack["logs"](), stack["logs"]()


@pytest.mark.spec("ARBORA-OPS-R001")
def test_the_tui_headless_prompt_completes(stack: dict[str, Any]) -> None:
    """The installed test dependency completes its headless prompt end to end."""
    exe = shutil.which("agent-terminal-ui", path=str(Path(sys.executable).parent))
    assert exe is not None, "install the locked test extra for TUI acceptance"
    env = {
        **{k: v for k, v in os.environ.items() if k in _INHERITED_ENV},
        "AGENT_URL": stack["base_url"],
        "AGENT_BEARER_TOKEN": stack["token"],
        "HOME": tempfile.mkdtemp(prefix="tui-home-"),
    }
    done = subprocess.run(
        [exe, "--headless", "--prompt", "hello"],
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}\n{stack['logs']()}"
    assert "https://example.org/arbora-boot" in done.stdout
