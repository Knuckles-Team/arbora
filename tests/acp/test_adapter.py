"""Real SDK/subprocess transport tests; model and authority are synthetic."""

import asyncio
import os
import sys
from pathlib import Path

import acp
import pytest
from acp import schema

pytestmark = pytest.mark.spec("ARBORA-ACP-R001.9.1")


class Client:
    def __init__(self):
        self.updates = []
        self.updated = asyncio.Event()
        self.reads = []
        self.waiting = asyncio.Event()
        self.read_started = asyncio.Event()
        self.block_reads = False

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)
        self.updated.set()
        if getattr(getattr(update, "content", None), "text", None) == "waiting":
            self.waiting.set()

    async def wait_for_text(self, expected: str) -> None:
        # SDK notification callbacks may finish after the prompt response.
        # Wait for the exact observable text, without weakening its assertion.
        async with asyncio.timeout(3):
            while True:
                self.updated.clear()
                text = "".join(
                    update.content.text
                    for update in self.updates
                    if update.session_update == "agent_message_chunk"
                )
                if text == expected:
                    return
                await self.updated.wait()

    async def read_text_file(self, session_id, path, **kwargs):
        self.reads.append(path)
        self.read_started.set()
        if self.block_reads:
            await asyncio.Event().wait()
        return schema.ReadTextFileResponse(content=Path(path).read_text())


@pytest.fixture
async def connected(tmp_path, request):
    client = Client()
    (tmp_path / "test.txt").write_text("client file text")
    script = Path(__file__).parent / "fixtures/stdio_host.py"
    async with acp.spawn_agent_process(
        client,
        sys.executable,
        str(script),
        str(tmp_path),
        str(getattr(request, "param", 4096)),
        env=dict(os.environ),
    ) as (connection, process):
        async with asyncio.timeout(10):
            initialized = await connection.initialize(
                protocol_version=1,
                client_capabilities=schema.ClientCapabilities(
                    fs=schema.FileSystemCapabilities(
                        read_text_file=True, write_text_file=True
                    ),
                ),
            )
        assert not initialized.agent_capabilities.load_session
        yield client, connection, str(tmp_path)


async def test_real_stdio_streams_au_text_and_reads_client(connected):
    client, conn, root = connected
    session = await conn.new_session(cwd=root, mcp_servers=[])
    result = await conn.prompt(
        session_id=session.session_id, prompt=[acp.text_block("hello")]
    )
    assert result.stop_reason == "end_turn"
    await client.wait_for_text("hello client")
    assert (
        "".join(
            u.content.text
            for u in client.updates
            if u.session_update == "agent_message_chunk"
        )
        == "hello client"
    )
    client.updates.clear()
    result = await conn.prompt(
        session_id=session.session_id, prompt=[acp.text_block("read")]
    )
    assert result.stop_reason == "end_turn"
    await client.wait_for_text("client file text")
    assert client.reads == [str(Path(root) / "test.txt")]
    assert (
        "".join(
            u.content.text
            for u in client.updates
            if u.session_update == "agent_message_chunk"
        )
        == "client file text"
    )


async def test_real_stdio_cancel_stops_run_then_accepts_new_prompt(connected):
    client, conn, root = connected
    session = await conn.new_session(cwd=root, mcp_servers=[])
    pending = asyncio.create_task(
        conn.prompt(session_id=session.session_id, prompt=[acp.text_block("wait")])
    )
    await asyncio.wait_for(client.waiting.wait(), 3)
    await conn.cancel(session_id=session.session_id)
    result = await asyncio.wait_for(pending, 3)
    assert result.stop_reason == "cancelled"
    result = await conn.prompt(
        session_id=session.session_id, prompt=[acp.text_block("hello")]
    )
    assert result.stop_reason == "end_turn"


async def test_real_stdio_refuses_effects_recovery_and_unknown_session(connected):
    client, conn, root = connected
    with pytest.raises(acp.RequestError):
        await conn.new_session(cwd="/untrusted", mcp_servers=[])
    session = await conn.new_session(cwd=root, mcp_servers=[])
    await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("write")])
    text = "".join(
        u.content.text
        for u in client.updates
        if u.session_update == "agent_message_chunk"
    )
    assert "permission_required" in text
    previews = [u for u in client.updates if u.session_update == "tool_call"]
    assert previews and previews[-1].status == "failed"
    assert previews[-1].content[0].type == "diff"
    assert not client.reads
    with pytest.raises(acp.RequestError):
        await conn.load_session(session_id=session.session_id, cwd=root, mcp_servers=[])
    with pytest.raises(acp.RequestError):
        await conn.prompt(session_id="unknown", prompt=[acp.text_block("hello")])


@pytest.fixture
def admitted_agent():
    from datetime import timedelta

    from agent_utilities.runtime import RunRequest

    from arbora.acp.adapter import HostedACPAgent
    from arbora.acp.session_policy import AcpSessionPolicy, PrincipalContext

    principal = [PrincipalContext("user", "tenant", frozenset({"kg:write"}))]
    calls = []

    def factory(identity, session_id, text):
        calls.append((identity, session_id, text))
        return RunRequest(task=text)

    agent = HostedACPAgent(
        lambda: principal[0],
        policy=AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
        trusted_roots=frozenset({"/workspace"}),
        request_factory=factory,
        max_sessions=1,
        max_prompt_bytes=8,
    )
    agent.on_connect(Client())
    return agent, principal, calls


@pytest.mark.parametrize("change", ["principal", "tenant", "scope", "absent"])
async def test_changed_authority_never_reaches_request_factory(admitted_agent, change):
    from arbora.acp.session_policy import PrincipalContext

    agent, principal, calls = admitted_agent
    await agent.initialize(protocol_version=1)
    session = await agent.new_session(cwd="/workspace")
    principal[0] = {
        "principal": PrincipalContext("other", "tenant", frozenset({"kg:write"})),
        "tenant": PrincipalContext("user", "other", frozenset({"kg:write"})),
        "scope": PrincipalContext("user", "tenant", frozenset()),
        "absent": None,
    }[change]
    with pytest.raises(acp.RequestError) as caught:
        await agent.prompt(session.session_id, [acp.text_block("hello")])
    assert caught.value.data["reason"] in {"unauthenticated", "principal_mismatch"}
    assert not calls
    await agent.aclose()


async def test_session_and_prompt_bounds_fail_before_model(admitted_agent):
    agent, _, calls = admitted_agent
    with pytest.raises(acp.RequestError):
        await agent.new_session(cwd="/workspace")
    await agent.initialize(protocol_version=1)
    with pytest.raises(acp.RequestError):
        await agent.initialize(protocol_version=1)
    session = await agent.new_session(cwd="/workspace")
    with pytest.raises(acp.RequestError):
        await agent.new_session(cwd="/workspace")
    for prompt in [
        [],
        [acp.text_block("x" * 9)],
        [acp.image_block("AA==", "image/png")],
    ]:
        with pytest.raises(acp.RequestError):
            await agent.prompt(session.session_id, prompt)
    assert not calls
    await agent.close_session(session.session_id)
    await agent.new_session(cwd="/workspace")
    await agent.aclose()


async def test_missing_host_factory_fails_closed(admitted_agent):
    from datetime import timedelta

    from arbora.acp.adapter import HostedACPAgent
    from arbora.acp.session_policy import AcpSessionPolicy

    _, principal, _ = admitted_agent
    agent = HostedACPAgent(
        lambda: principal[0],
        policy=AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
        trusted_roots=frozenset({"/workspace"}),
    )
    agent.on_connect(Client())
    await agent.initialize(protocol_version=1)
    with pytest.raises(acp.RequestError) as caught:
        await agent.new_session(cwd="/workspace")
    assert caught.value.data == {"reason": "chat_adapter_unavailable"}
    await agent.aclose()


async def test_real_stdio_refuses_path_escape_and_output_overflow(connected):
    client, conn, root = connected
    session = await conn.new_session(cwd=root, mcp_servers=[])
    await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("escape")])
    text = "".join(
        u.content.text
        for u in client.updates
        if u.session_update == "agent_message_chunk"
    )
    assert "permission_denied" in text
    assert not client.reads
    client.updates.clear()
    with pytest.raises(acp.RequestError):
        await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("large")]
        )
    assert sum(len(u.model_dump_json().encode()) for u in client.updates) <= 4096


async def test_expired_owner_can_cancel_and_close_without_reopening(
    admitted_agent, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from arbora.acp import session_policy

    moments = [datetime(2026, 1, 1, tzinfo=UTC)]

    class Clock:
        @staticmethod
        def now(tz):
            return moments[0]

    monkeypatch.setattr(session_policy, "datetime", Clock)
    agent, _, calls = admitted_agent
    await agent.initialize(protocol_version=1)
    session = await agent.new_session(cwd="/workspace")
    moments[0] += timedelta(hours=2)
    with pytest.raises(acp.RequestError):
        await agent.prompt(session.session_id, [acp.text_block("hello")])
    await agent.cancel(session.session_id)
    await agent.close_session(session.session_id)
    assert not calls
    await agent.aclose()


@pytest.mark.parametrize("connected", [700], indirect=True)
async def test_cancel_during_client_read_preserves_cancellation(connected):
    client, conn, root = connected
    client.block_reads = True
    session = await conn.new_session(cwd=root, mcp_servers=[])
    pending = asyncio.create_task(
        conn.prompt(session_id=session.session_id, prompt=[acp.text_block("read")])
    )
    await asyncio.wait_for(client.read_started.wait(), 3)
    await conn.cancel(session_id=session.session_id)
    result = await asyncio.wait_for(pending, 3)
    assert result.stop_reason == "cancelled"
    assert len(client.reads) == 1
    assert all(u.session_update != "agent_message_chunk" for u in client.updates)


async def test_oversized_preview_is_not_sent_and_cannot_write(connected):
    client, conn, root = connected
    session = await conn.new_session(cwd=root, mcp_servers=[])
    await conn.prompt(
        session_id=session.session_id, prompt=[acp.text_block("bigwrite")]
    )
    assert not [u for u in client.updates if u.session_update == "tool_call"]
    assert (Path(root) / "test.txt").read_text() == "client file text"
    assert sum(len(u.model_dump_json().encode()) for u in client.updates) <= 4096


async def test_adapter_cannot_rebind_connection(admitted_agent):
    agent, _, _ = admitted_agent
    with pytest.raises(RuntimeError, match="cannot be rebound"):
        agent.on_connect(Client())
    await agent.initialize(protocol_version=1)
    await agent.aclose()
    with pytest.raises(RuntimeError, match="cannot be rebound"):
        agent.on_connect(Client())


async def test_real_stdio_completed_text_history_isolated_and_cancelled_turn_absent(
    connected,
):
    import json

    client, conn, root = connected
    first = await conn.new_session(cwd=root, mcp_servers=[])
    await conn.prompt(session_id=first.session_id, prompt=[acp.text_block("hello")])
    pending = asyncio.create_task(
        conn.prompt(session_id=first.session_id, prompt=[acp.text_block("wait")])
    )
    await asyncio.wait_for(client.waiting.wait(), 3)
    with pytest.raises(acp.RequestError):
        await conn.prompt(
            session_id=first.session_id, prompt=[acp.text_block("concurrent")]
        )
    await conn.cancel(session_id=first.session_id)
    assert (await asyncio.wait_for(pending, 3)).stop_reason == "cancelled"
    client.updates.clear()
    await conn.prompt(session_id=first.session_id, prompt=[acp.text_block("history")])
    text = "".join(
        u.content.text
        for u in client.updates
        if u.session_update == "agent_message_chunk"
    )
    assert json.loads(text) == ["hello", "history"]
    client.updates.clear()
    other = await conn.new_session(cwd=root, mcp_servers=[])
    await conn.prompt(session_id=other.session_id, prompt=[acp.text_block("history")])
    text = "".join(
        u.content.text
        for u in client.updates
        if u.session_update == "agent_message_chunk"
    )
    assert json.loads(text) == ["history"]


@pytest.mark.parametrize("when", ["stream", "completion"])
async def test_authority_loss_stops_updates_and_completion(when):
    from dataclasses import replace
    from datetime import timedelta

    from agent_utilities import host_ports
    from agent_utilities.runtime import RunPlan, RunRequest
    from pydantic_ai.models.function import FunctionModel

    from arbora.acp.adapter import HostedACPAgent
    from arbora.acp.session_policy import AcpSessionPolicy, PrincipalContext

    principal = [PrincipalContext("user", "tenant", frozenset({"kg:write"}))]
    gate = asyncio.Event()
    client = Client()
    commits = []

    async def model(messages, info):
        yield "waiting"
        await gate.wait()
        principal[0] = PrincipalContext("other", "tenant", frozenset({"kg:write"}))
        if when == "stream":
            yield "must not emit"

    async def completed(request, result):
        commits.append(result)

    plan = RunPlan(model=FunctionModel(stream_function=model), steps=("prompt",))
    plan = replace(plan, harness=replace(plan.harness, plan_scaffold="off"))
    previous = host_ports.run_decision_provider()
    host_ports.register_run_decision_provider(lambda request: plan)
    agent = HostedACPAgent(
        lambda: principal[0],
        policy=AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
        trusted_roots=frozenset({"/workspace"}),
        request_factory=lambda identity, session_id, text: RunRequest(task=text),
        completed=completed,
    )
    agent.on_connect(client)
    try:
        await agent.initialize(protocol_version=1)
        session = await agent.new_session(cwd="/workspace")
        pending = asyncio.create_task(
            agent.prompt(session.session_id, [acp.text_block("prompt")])
        )
        await asyncio.wait_for(client.waiting.wait(), 3)
        gate.set()
        with pytest.raises(acp.RequestError):
            await pending
        assert not commits
        assert "".join(u.content.text for u in client.updates) == "waiting"
    finally:
        await agent.aclose()
        host_ports.register_run_decision_provider(previous)


async def test_completion_save_failure_never_acknowledges_success(connected):
    client, conn, root = connected
    session = await conn.new_session(cwd=root, mcp_servers=[])
    with pytest.raises(acp.RequestError) as failure:
        await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("savefail")]
        )
    assert "private storage detail" not in str(failure.value)
    assert client.updates  # Streamed text is not a persisted completion receipt.


@pytest.mark.parametrize("whole_adapter", [False, True])
async def test_closing_session_cannot_admit_new_work(admitted_agent, whole_adapter):
    agent, _, calls = admitted_agent
    entered, release = asyncio.Event(), asyncio.Event()

    async def closing(session_id):
        entered.set()
        await release.wait()

    agent._closed = closing
    await agent.initialize(protocol_version=1)
    session = await agent.new_session(cwd="/workspace")
    pending = asyncio.create_task(
        agent.aclose() if whole_adapter else agent.close_session(session.session_id)
    )
    await asyncio.wait_for(entered.wait(), 3)
    try:
        with pytest.raises(acp.RequestError):
            await agent.prompt(session.session_id, [acp.text_block("hello")])
        if whole_adapter:
            with pytest.raises(acp.RequestError):
                await agent.new_session(cwd="/workspace")
        assert not calls
    finally:
        release.set()
        await pending
        await agent.aclose()


async def test_authority_rechecked_after_read_preview(admitted_agent):
    from arbora.acp.adapter import _ReadTransport
    from arbora.acp.session_policy import PrincipalContext

    agent, principal, _ = admitted_agent
    client = agent._client
    await agent.initialize(
        protocol_version=1,
        client_capabilities=schema.ClientCapabilities(
            fs=schema.FileSystemCapabilities(read_text_file=True)
        ),
    )
    session = await agent.new_session(cwd="/workspace")

    async def update(*args, **kwargs):
        principal[0] = PrincipalContext("other", "tenant", frozenset({"kg:write"}))

    client.session_update = update
    with pytest.raises(acp.RequestError):
        await _ReadTransport(agent, session.session_id).call(
            "fs/read_text_file", {"path": "test.txt"}
        )
    assert not client.reads
    await agent.aclose()


async def test_real_stdio_close_refuses_subsequent_prompt(connected):
    _, conn, root = connected
    session = await conn.new_session(cwd=root)
    await conn.close_session(session_id=session.session_id)
    with pytest.raises(acp.RequestError):
        await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("hello")]
        )
