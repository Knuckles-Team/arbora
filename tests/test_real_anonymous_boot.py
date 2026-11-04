"""Opt-in real anonymous serving gate: only the model is simulated.

Set ARBORA_NOAUTH_TEST_ENGINE_BINARY to a matching built EG server. This test
never manufactures credentials, substitutes graph storage, or relaxes startup
contract admission. The ordinary authenticated real-boot suite is separate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from tests.test_real_boot import _free_port, _listening, _terminate, _wait

pytestmark = [pytest.mark.real_boot, pytest.mark.spec("ARBORA-IDENTITY-R004.3")]


@pytest.fixture(scope="module")
def anonymous_stack(tmp_path_factory):
    binary = os.getenv("ARBORA_NOAUTH_TEST_ENGINE_BINARY")
    if not binary:
        pytest.skip("select a matching no-auth native EG binary explicitly")
    root = tmp_path_factory.mktemp("anonymous-real-boot")
    ports = {key: _free_port() for key in ("engine", "model", "arbora")}
    base_url = f"http://127.0.0.1:{ports['arbora']}"
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LD_LIBRARY_PATH", "PYTHONPATH", "SSL_CERT_FILE"}
    }
    base_env.update(
        {
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "ARBORA_AUTH_MODE": "none",
                "DEPLOYMENT_PROFILE": "single-node-prod",
                "GRAPH_SERVICE_ENDPOINTS": f"tcp://127.0.0.1:{ports['engine']}",
                "ENABLE_WEB_UI": False,
                "MESSAGING_INTAKE_ENABLED": False,
                "MCP_HTTP_ALLOWED_PRIVATE_HOSTS": "127.0.0.1",
                "MODEL_HTTP_ALLOWED_PRIVATE_HOSTS": "127.0.0.1",
                "EMBEDDING_MODELS": "[]",
                "CHAT_MODELS": json.dumps(
                    [
                        {
                            "id": "boot-model",
                            "base_url": f"http://127.0.0.1:{ports['model']}/v1",
                            "api_key_env": "SIMULATOR_PLACEHOLDER",
                        }
                    ]
                ),
            }
        )
    )
    (config_dir / "mcp_config.json").write_text(json.dumps({"mcpServers": {}}))
    env = {
        **base_env,
        "ARBORA_CONFIG_DIR": str(config_dir),
        "ARBORA_AUTH_MODE": "none",
        "SIMULATOR_PLACEHOLDER": "inert-local-simulator-not-a-credential",
    }
    processes = []
    logs = []
    bindir = str(Path(sys.executable).parent)

    def start(name, command, child_env):
        logfile = (root / f"{name}.log").open("w")
        logs.append(logfile)
        process = subprocess.Popen(
            command, env=child_env, stdout=logfile, stderr=subprocess.STDOUT
        )
        processes.append(process)
        return process

    def ready(port, process):
        if process.poll() is not None:
            raise AssertionError(f"process exited; logs in {root}")
        return _listening(port)

    try:
        engine = start(
            "eg",
            [
                binary,
                "--auth-mode",
                "none",
                "--tcp-only",
                "--tcp-addr",
                f"127.0.0.1:{ports['engine']}",
                "--persist-dir",
                str(root / "store"),
            ],
            {
                **base_env,
                "MEDULLA_CONNECTOR_PACK_IMPORTERS": "arbora=principal:sha256:"
                + hashlib.sha256(b"anonymous").hexdigest(),
            },
        )
        _wait(
            lambda: ready(ports["engine"], engine), timeout=60, what="anonymous engine"
        )
        model = start(
            "model",
            [
                sys.executable,
                str(Path(__file__).with_name("_boot_model.py")),
                str(ports["model"]),
            ],
            {**base_env, "BOOT_MODEL_RECEIPTS": str(root / "model-receipts.jsonl")},
        )
        _wait(lambda: ready(ports["model"], model), timeout=30, what="simulated model")
        ops = shutil.which("arbora-production-ops", path=bindir)
        assert ops is not None
        for operation in ("provision-semantic-content", "onboard-fleet"):
            result = subprocess.run(
                [ops, operation, "--served-url", base_url + "/mcp"],
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert result.returncode == 0, result.stdout + result.stderr
        entry = shutil.which("arbora", path=bindir)
        assert entry is not None
        server = start(
            "arbora",
            [
                entry,
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                str(ports["arbora"]),
                "--auth-type",
                "none",
            ],
            env,
        )
        _wait(
            lambda: ready(ports["arbora"], server), timeout=180, what="anonymous Arbora"
        )
        yield {
            "url": base_url,
            "env": base_env,
            "server_env": env,
            "root": root,
            "engine_addr": f"127.0.0.1:{ports['engine']}",
        }
    finally:
        for process in reversed(processes):
            _terminate(process)
        for logfile in logs:
            logfile.close()


def test_real_tokenless_read_and_http_admission(anonymous_stack):
    with httpx.Client(
        trust_env=False, base_url=anonymous_stack["url"], timeout=30
    ) as client:
        response = client.post(
            "/api/v1/ops/query.uql", json={"text": "MATCH (:BootProbe) |> LIMIT 5"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True
        assert response.headers["x-arbora-auth-mode"] == "none"
        for headers in (
            {"Authorization": "Bearer forged"},
            {"Host": "evil.example"},
            {"Origin": "http://127.0.0.1"},
        ):
            assert client.get("/health", headers=headers).status_code == 403


def test_actual_tui_completes_tokenless_chat(anonymous_stack):
    entry = shutil.which("tuig", path=str(Path(sys.executable).parent))
    assert entry is not None
    nonce = "proof-" + uuid.uuid4().hex
    result = subprocess.run(
        [entry, "--headless", "--prompt", "hello anonymous real graph " + nonce],
        env={
            **anonymous_stack["env"],
            "AGENT_AUTH_MODE": "none",
            "AGENT_URL": anonymous_stack["url"],
        },
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = "https://example.org/arbora-boot for details. " + nonce
    assert expected in result.stdout
    receipts = [
        json.loads(line)
        for line in (anonymous_stack["root"] / "model-receipts.jsonl")
        .read_text()
        .splitlines()
    ]
    assert any(row["nonce"] == nonce and expected in row["reply"] for row in receipts)
    with httpx.Client(
        trust_env=False, base_url=anonymous_stack["url"], timeout=30
    ) as client:
        response = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "proof-list",
                "method": "tasks/list",
                "params": {"limit": 50},
            },
        )
        response.raise_for_status()
        tasks = response.json()["result"]["tasks"]
        matching = []
        for row in tasks:
            detail = client.post(
                "/a2a",
                json={
                    "jsonrpc": "2.0",
                    "id": "proof-get",
                    "method": "tasks/get",
                    "params": {"id": row["id"]},
                },
            )
            detail.raise_for_status()
            task = detail.json()["result"]
            if nonce in json.dumps(task.get("artifacts")):
                matching.append(task)
        assert len(matching) == 1
        assert matching[0]["status"]["state"] == "completed"


@pytest.mark.asyncio
async def test_real_tokenless_mcp_query(anonymous_stack):
    from fastmcp import Client

    async with Client(anonymous_stack["url"] + "/mcp") as client:
        tools = await client.list_tools()
        assert "ask" in {tool.name for tool in tools}
        result = await client.call_tool(
            "ask",
            {"op": "query.uql", "params": {"text": "MATCH (:BootProbe) |> LIMIT 5"}},
        )
        assert result.structured_content["ok"] is True


def test_real_effect_journal_and_provenance_binding(anonymous_stack):
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("_boot_anonymous_workitems.py")),
            anonymous_stack["engine_addr"],
        ],
        env=anonymous_stack["server_env"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence["effect_reserve_complete_replay_conflict"] is True
    assert evidence["none_provenance_accepted"] is True
    assert len(evidence["authority_refusals"]) == 4


@pytest.mark.asyncio
async def test_semantic_projection_matches_committed_pack(anonymous_stack):
    from medulla import MedullaClient
    from medulla.generated.connector_pack import ConnectorPackStatusRequest
    from medulla.generated.reasoning import send_graph_schema_list
    from medulla.generated.storage import send_connector_pack_status

    client = await MedullaClient.connect(
        auth_mode="none",
        tcp_addr=anonymous_stack["engine_addr"],
        graph_name="tenant__local",
    )
    try:
        status = await send_connector_pack_status(
            client,
            ConnectorPackStatusRequest(connector="arbora", tenant_id="local"),
            "tenant__local",
        )
        assert status.head is not None and status.projection.projection == "applied"
        assert status.head.visible_record_id == status.head.record_id
        schema = await send_graph_schema_list(client, {}, status.projection.graph)
        sources = [
            source
            for source in schema.dynamic_sources
            if source.origin.origin == "pack"
            and source.origin.connector == "arbora"
            and source.origin.record_id == status.head.record_id
        ]
        assert sources
        for source in sources:
            assert source.shapes_bytes > 0 and source.shapes_sha256
            if source.ontology_bytes:
                assert source.ontology_sha256
    finally:
        await client.close()


@pytest.mark.spec("ARBORA-A2A-R001", "ARBORA-HOST-R001.2.1")
@pytest.mark.parametrize("reply_case", ["maximum_astral", "oversized_terminal"])
def test_native_bounded_reply_events(anonymous_stack, reply_case):
    """Only the model is fake; reply reads and stored events come from real EG."""
    from arbora.agent_runs import (
        MAX_RUN_EVENT_PAYLOAD_BYTES,
        RUN_EVENT_PAYLOAD_ERROR_SCHEMA,
    )
    from tests._boot_model import (
        MAX_ASTRAL_REPLY,
        MAX_ASTRAL_TRIGGER,
        OVERSIZED_EVENT_REPLY,
        OVERSIZED_EVENT_TRIGGER,
    )

    oversized = reply_case == "oversized_terminal"
    trigger = OVERSIZED_EVENT_TRIGGER if oversized else MAX_ASTRAL_TRIGGER
    expected = OVERSIZED_EVENT_REPLY if oversized else MAX_ASTRAL_REPLY
    nonce = "proof-" + uuid.uuid4().hex
    with httpx.Client(
        trust_env=False, base_url=anonymous_stack["url"], timeout=180
    ) as client:
        sent = client.post(
            "/a2a",
            headers={"Idempotency-Key": nonce},
            json={
                "jsonrpc": "2.0",
                "id": nonce,
                "method": "message/send",
                "params": {
                    "message": {
                        "role": "user",
                        "messageId": nonce,
                        "parts": [{"kind": "text", "text": f"{trigger} {nonce}"}],
                    }
                },
            },
        )
        sent.raise_for_status()
        task_id = sent.json()["result"]["id"]
        observed = {}

        def completed():
            response = client.post(
                "/a2a",
                json={
                    "jsonrpc": "2.0",
                    "id": nonce,
                    "method": "tasks/get",
                    "params": {"id": task_id},
                },
            )
            response.raise_for_status()
            observed["task"] = response.json()["result"]
            return observed["task"]["status"]["state"] in {
                "completed",
                "failed",
                "canceled",
                "rejected",
            }

        _wait(completed, timeout=90, what=f"native bounded reply {reply_case}")
        task = observed["task"]
        assert task["status"]["state"] == "completed"
        if oversized:
            assert task.get("artifacts") is None
            assert task["metadata"]["arbora"]["reply"] == {
                "status": "unavailable",
                "reason": "payload_too_large",
            }
        else:
            assert len(task["artifacts"]) == 1
            assert task["artifacts"][0]["parts"] == [{"kind": "text", "text": expected}]
            assert len(expected) == 65_536

    receipts = [
        json.loads(line)
        for line in (anonymous_stack["root"] / "model-receipts.jsonl")
        .read_text()
        .splitlines()
    ]
    assert any(row["nonce"] == nonce and row["reply"] == expected for row in receipts)

    async def persisted_event():
        from medulla import MedullaClient

        graph = await MedullaClient.connect(
            auth_mode="none",
            tcp_addr=anonymous_stack["engine_addr"],
            graph_name="tenant__local",
        )
        try:
            identity = json.dumps(task_id)
            rows = await graph.query.cypher_read(
                "MATCH (e) WHERE e.type = 'RunEvent' "
                f"AND e.run_id = {identity} AND e.task_id = {identity} "
                "AND e.event_type = 'graph_complete' "
                "RETURN e.payload AS payload LIMIT 2"
            )
            assert len(rows) == 1
            payload = rows[0]["payload"]
            assert len(payload.encode("utf-8")) <= MAX_RUN_EVENT_PAYLOAD_BYTES
            return json.loads(payload)
        finally:
            await graph.close()

    event = asyncio.run(persisted_event())
    assert event["run_id"] == task_id and event["type"] == "graph_complete"
    if oversized:
        assert len(json.dumps(expected).encode("utf-8")) > MAX_RUN_EVENT_PAYLOAD_BYTES
        assert event["schema"] == RUN_EVENT_PAYLOAD_ERROR_SCHEMA
        assert event["status"] == "completed"
        assert event["payload_error"] == {
            "code": "payload_too_large",
            "max_bytes": MAX_RUN_EVENT_PAYLOAD_BYTES,
        }
        assert "result" not in event
    else:
        assert event["result"]["output"] == expected
        assert event["result"]["run_id"] == task_id
