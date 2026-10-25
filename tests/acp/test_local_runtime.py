"""The internal local backend is explicit, ephemeral and fails closed."""

import asyncio
import os
import sys

import acp
import pytest

from tests.acp.test_adapter import Client

pytestmark = pytest.mark.spec("ARBORA-ACP-R001.9.1")


@pytest.mark.parametrize(
    "args",
    [
        ["--model", "missing:unused", "--workspace-root", "/workspace"],
        [
            "--local",
            "--model",
            "missing:unused",
            "--workspace-root",
            "/workspace",
            "--timeout-seconds",
            "nan",
        ],
        ["--local", "--model", "missing:unused", "--workspace-root", "relative"],
    ],
)
async def test_invalid_startup_never_emits_protocol_success(args):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "arbora.acp.local_runtime",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ),
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode != 0
    assert stdout == b""
    assert stderr


async def test_real_local_backend_identity_and_failed_turn_without_replay(tmp_path):
    # Unknown model provider guarantees no provider network call or credential use.
    async with acp.spawn_agent_process(
        Client(),
        sys.executable,
        "-m",
        "arbora.acp.local_runtime",
        "--local",
        "--model",
        "missing:unused",
        "--workspace-root",
        str(tmp_path),
        env=dict(os.environ),
    ) as (connection, process):
        async with asyncio.timeout(10):
            initialized = await connection.initialize(protocol_version=1)
            assert not initialized.agent_capabilities.load_session
            session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
            with pytest.raises(acp.RequestError):
                await connection.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("hello")]
                )
            with pytest.raises(acp.RequestError) as caught:
                await connection.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("continue")]
                )
            assert str(caught.value) == "Run failed"
    assert process.returncode is not None


async def test_local_identity_renewal_keeps_same_binding(monkeypatch):
    import argparse

    from agent_utilities import host_ports

    from arbora.acp.local_runtime import compose_local
    from arbora.api.contracts.session import GraphSession, SessionExpiredError
    from arbora.security import request_identity

    issued = []
    expired = [False]
    original_mint = request_identity.mint_local_process_session
    original_check = GraphSession.ensure_authority_current

    def mint():
        session = original_mint()
        issued.append(session)
        return session

    def check(self, **kwargs):
        if issued and self is issued[0] and expired[0]:
            raise SessionExpiredError("test expiry")
        return original_check(self, **kwargs)

    monkeypatch.setattr(request_identity, "mint_local_process_session", mint)
    monkeypatch.setattr(GraphSession, "ensure_authority_current", check)
    previous = host_ports.run_decision_provider()
    host_ports.register_run_decision_provider(None)
    agent = compose_local(
        argparse.Namespace(
            model="missing:unused",
            workspace_root="/workspace",
            request_limit=4,
            timeout_seconds=10,
            client_reads=False,
        )
    )
    agent.on_connect(Client())
    try:
        await agent.initialize(protocol_version=1)
        session = await agent.new_session(cwd="/workspace")
        expired[0] = True
        await agent.cancel(session.session_id)
        await agent.close_session(session.session_id)
        assert len(issued) == 2
        assert issued[0].actor.actor_id == issued[1].actor.actor_id
        assert issued[0].tenant == issued[1].tenant
        assert issued[0].scopes == issued[1].scopes
    finally:
        await agent.aclose()
        host_ports.register_run_decision_provider(previous)
        host_ports.register_run_history_provider(None)


@pytest.mark.parametrize(
    "payload,expected",
    [(b"", 0), (b'{"jsonrpc":"2.0"', 1), (b"private-marker" * 6000 + b"\n", 1)],
)
async def test_stdio_eof_truncated_and_oversized_frames_are_bounded(
    payload, expected, tmp_path
):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "arbora.acp.local_runtime",
        "--local",
        "--model",
        "missing:unused",
        "--workspace-root",
        str(tmp_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ),
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(payload), 10)
    assert process.returncode == expected
    assert stdout == b""
    assert b"private-marker" not in stderr


async def test_real_local_composition_success_history_cancel_and_close(tmp_path):
    import json
    from pathlib import Path

    client = Client()
    script = Path(__file__).parent / "fixtures/local_composed_host.py"
    async with acp.spawn_agent_process(
        client,
        sys.executable,
        str(script),
        "--local",
        "--model",
        "scripted:local",
        "--workspace-root",
        str(tmp_path),
        env=dict(os.environ),
    ) as (conn, process):
        async with asyncio.timeout(15):
            await conn.initialize(protocol_version=1)
            session = await conn.new_session(cwd=str(tmp_path))
            assert (
                await conn.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("hello")]
                )
            ).stop_reason == "end_turn"
            assert "".join(u.content.text for u in client.updates) == "local answer"
            client.updates.clear()
            assert (
                await conn.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("history")]
                )
            ).stop_reason == "end_turn"
            assert json.loads("".join(u.content.text for u in client.updates)) == [
                "hello",
                "history",
            ]
            client.updates.clear()
            pending = asyncio.create_task(
                conn.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("wait")]
                )
            )
            await client.waiting.wait()
            await conn.cancel(session_id=session.session_id)
            assert (await pending).stop_reason == "cancelled"
            client.updates.clear()
            await conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("history")]
            )
            assert json.loads("".join(u.content.text for u in client.updates)) == [
                "hello",
                "history",
                "history",
            ]
            await conn.close_session(session_id=session.session_id)
            with pytest.raises(acp.RequestError):
                await conn.prompt(
                    session_id=session.session_id,
                    prompt=[acp.text_block("after close")],
                )
    assert process.returncode is not None
