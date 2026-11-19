"""Workspace permission behavior for coding sessions."""

from datetime import UTC, datetime, timedelta

import pytest

from arbora.acp import (
    AcpRefusal,
    AcpRefusalReason,
    AcpSessionPolicy,
    ClientCapability,
    CodingSessionAdmission,
    CodingSessionRequest,
    PrincipalContext,
)


@pytest.mark.spec("ARBORA-ACP-R001.4")
def test_trusted_root_read_write_and_standing_grant() -> None:
    now = datetime(2026, 11, 19, tzinfo=UTC)
    admission = CodingSessionAdmission(
        AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1)),
        trusted_roots=frozenset({"/workspace"}),
    )
    session = admission.admit(
        PrincipalContext("alice", "tenant", frozenset()),
        CodingSessionRequest("/workspace", frozenset(ClientCapability)),
        chat_adapter_available=True,
        now=now,
    )
    assert (
        admission.decide_effect(
            session,
            "fs/read_text_file",
            client_connected=True,
            chat_adapter_available=True,
            now=now,
        ).outcome
        == "granted"
    )
    assert (
        admission.decide_effect(
            session,
            "fs/write_text_file",
            client_connected=True,
            chat_adapter_available=True,
            now=now,
        ).outcome
        == "permission_required"
    )
    assert (
        admission.decide_effect(
            session,
            "fs/write_text_file",
            client_connected=True,
            chat_adapter_available=True,
            permission_answer="allow_always",
            now=now,
        ).outcome
        == "granted"
    )
    assert (
        admission.decide_effect(
            session,
            "fs/write_text_file",
            client_connected=True,
            chat_adapter_available=True,
            now=now,
        ).outcome
        == "standing"
    )
    with pytest.raises(AcpRefusal) as refused:
        admission.decide_effect(
            session,
            "fs/delete",
            client_connected=True,
            chat_adapter_available=True,
            now=now,
        )
    assert refused.value.reason is AcpRefusalReason.CAPABILITY_NOT_DECLARED
