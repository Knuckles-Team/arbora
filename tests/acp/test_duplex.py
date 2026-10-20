"""Connection correlation and previews without authority fabrication."""

import asyncio
import json
from datetime import timedelta

import pytest

from arbora.acp import (
    AcpRefusal,
    AcpSessionPolicy,
    CodingSessionAdmission,
    CodingSessionRequest,
    PrincipalContext,
)
from arbora.acp.duplex import MAX_QUEUED_EVENTS, ClientChannel
from arbora.acp.run_adapter import PreviewTransport


async def refreshed():
    return None


def channel():
    admission = CodingSessionAdmission(
        AcpSessionPolicy(timedelta(seconds=30), timedelta(minutes=1)),
        trusted_roots=frozenset({"/project"}),
    )
    session = admission.admit(
        PrincipalContext("alice", "tenant", frozenset({"kg:write"})),
        CodingSessionRequest(
            "/project",
            frozenset({"fs/read_text_file", "fs/write_text_file", "terminal/create"}),
        ),
        chat_adapter_available=True,
    )
    value = ClientChannel("session", session, admission, refreshed)
    value.connected = True
    return value


@pytest.mark.spec("ARBORA-ACP-R001.9")
async def test_real_correlation_duplicate_and_sequence():
    value = channel()
    task = asyncio.create_task(value.request("fs/read_text_file", {"path": "a"}))
    events = value.events()
    assert await anext(events) == b": connected\n\n"
    wire = await anext(events)
    assert wire.startswith(b"id: 1\n")
    request = json.loads(wire.decode().split("data: ")[1])
    reply = {"jsonrpc": "2.0", "id": request["id"], "result": {"content": "original"}}
    value.reply(reply)
    assert await task == {"content": "original"}
    with pytest.raises(ValueError):
        value.reply(reply)
    await events.aclose()
    assert value.closed


@pytest.mark.spec("ARBORA-ACP-R001.9")
async def test_disconnect_and_queue_overflow_refuse():
    value = channel()
    for _ in range(MAX_QUEUED_EVENTS):
        value.emit({"notification": "bounded"})
    with pytest.raises(AcpRefusal, match="client_disconnected"):
        value.emit({"notification": "overflow"})
    assert value.closed
    with pytest.raises(AcpRefusal, match="client_disconnected"):
        await value.request("fs/read_text_file", {"path": "a"})


@pytest.mark.spec("ARBORA-ACP-R001.9")
async def test_command_is_only_a_preview():
    value = channel()
    with pytest.raises(AcpRefusal, match="permission_required"):
        await PreviewTransport(value).call(
            "terminal/create", {"command": "echo", "args": ["hi"]}
        )
    events = value.events()
    await anext(events)
    wire = await anext(events)
    payload = json.loads(wire.decode().split("data: ")[1])
    assert payload["method"] == "session/update"
    update = payload["params"]["update"]
    assert update["_meta"]["previewOnly"] is True
    assert update["status"] == "failed"
    assert not value._pending
    await events.aclose()
