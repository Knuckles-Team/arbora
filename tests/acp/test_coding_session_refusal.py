"""Typed refusal reasons return before any forwarded request (ARBORA-ACP-R001.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import acp
import pytest
from meristem.host_ports import ClientCapabilityRefused

from arbora.acp.adapter import HostedACPAgent, _EffectTransport, _refusal_error
from arbora.acp.coding_session import (
    ClientCapability,
    CodingSessionAdmission,
    CodingSessionRequest,
)
from arbora.acp.session_policy import (
    AcpRefusal,
    AcpRefusalReason,
    AcpSessionPolicy,
    PrincipalContext,
)

NOW = datetime(2026, 11, 26, tzinfo=UTC)
PRINCIPAL = PrincipalContext("alice", "tenant", frozenset())


class _Forwarded:
    """A client that records every request that reaches it."""

    def __init__(self, answer: str | None = "reject") -> None:
        self.calls: list[str] = []
        self.answer = answer

    async def request_permission(self, **kwargs: Any) -> Any:
        self.calls.append("permission")
        if self.answer is None:
            outcome = SimpleNamespace(outcome="cancelled")
        else:
            outcome = SimpleNamespace(outcome="selected", option_id=self.answer)
        return SimpleNamespace(outcome=outcome)

    async def write_text_file(self, **kwargs: Any) -> None:
        self.calls.append("write")

    async def create_terminal(self, **kwargs: Any) -> None:
        self.calls.append("terminal")


def _agent(capabilities: frozenset[ClientCapability]):
    agent = HostedACPAgent(
        lambda: PRINCIPAL,
        policy=AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
        trusted_roots=frozenset({"/workspace"}),
        request_factory=lambda *_: None,
    )
    agent._initialized = True
    session = agent._admission.admit(
        PRINCIPAL,
        CodingSessionRequest("/workspace", capabilities),
        chat_adapter_available=True,
        now=datetime.now(UTC),
    )
    agent._sessions["s"] = SimpleNamespace(
        admission=session, active=None, output_bytes=0
    )
    return agent


@pytest.mark.spec("ARBORA-ACP-R001.6")
def test_each_reason_is_typed_and_forwards_nothing() -> None:
    # Every reason maps to ACP error -32001 with data.reason.
    for reason in AcpRefusalReason:
        error = _refusal_error(reason)
        assert error.code == -32001 and error.data == {"reason": reason.value}
    assert AcpRefusalReason.PERMISSION_DENIED.value == "permission_denied"

    admission = CodingSessionAdmission(
        AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
        trusted_roots=frozenset({"/workspace"}),
    )
    declared = frozenset({ClientCapability.WRITE_TEXT_FILE})
    session = admission.admit(
        PRINCIPAL,
        CodingSessionRequest("/workspace", declared),
        chat_adapter_available=True,
        now=NOW,
    )
    # A method outside the declared set, and no client connected, refuse by reason.
    with pytest.raises(AcpRefusal) as undeclared:
        admission.decide_effect(
            session,
            "terminal/create",
            client_connected=True,
            chat_adapter_available=True,
            now=NOW,
        )
    assert undeclared.value.reason is AcpRefusalReason.CAPABILITY_NOT_DECLARED
    with pytest.raises(AcpRefusal) as disconnected:
        admission.decide_effect(
            session,
            "fs/write_text_file",
            client_connected=False,
            chat_adapter_available=True,
            now=NOW,
        )
    assert disconnected.value.reason is AcpRefusalReason.CLIENT_DISCONNECTED

    # A reject, and a cancel, deny with PERMISSION_DENIED: nothing is forwarded.
    import asyncio

    for answer in ("reject", None):
        agent = _agent(declared)
        client = _Forwarded(answer)
        agent._client = client  # type: ignore[assignment]
        transport = _EffectTransport(agent, "s")
        with pytest.raises(ClientCapabilityRefused) as denied:
            asyncio.run(
                transport.call(
                    "fs/write_text_file", {"path": "/workspace/a", "content": "x"}
                )
            )
        assert "permission_denied" in str(denied.value)
        assert client.calls == ["permission"]

    # An undeclared terminal refuses with no request at all.
    agent = _agent(declared)
    client = _Forwarded()
    agent._client = client  # type: ignore[assignment]
    with pytest.raises(ClientCapabilityRefused):
        asyncio.run(_EffectTransport(agent, "s").call("terminal/create", {}))
    assert client.calls == []
    assert isinstance(_refusal_error(AcpRefusalReason.UNTRUSTED_ROOT), acp.RequestError)
