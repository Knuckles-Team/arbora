"""Coding admission producer checks, without a transport or permission issuer."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arbora.acp import (
    AcpRefusal,
    AcpRefusalReason,
    AcpSessionPolicy,
    ClientCapability,
    CodingSessionAdmission,
    CodingSessionRequest,
    PrincipalContext,
    WorkspaceGrant,
)

ROOT = "/workspace/project"
NOW = datetime(2026, 10, 17, tzinfo=UTC)
POLICY = AcpSessionPolicy(timedelta(minutes=5), timedelta(hours=1))
PRINCIPAL = PrincipalContext("alice", "tenant", frozenset({"chat:use"}))


def admission() -> CodingSessionAdmission:
    return CodingSessionAdmission(POLICY, trusted_roots=frozenset({ROOT}))


def request() -> CodingSessionRequest:
    return CodingSessionRequest(ROOT, frozenset(ClientCapability))


@pytest.mark.spec("GRAPHOS-ACP-R001.3")
def test_frozen_model_and_no_grant_injection() -> None:
    declared: Any = {ClientCapability.READ_TEXT_FILE}
    value = CodingSessionRequest(ROOT, declared)
    declared.clear()
    assert value.client_capabilities == frozenset({ClientCapability.READ_TEXT_FILE})
    grant = WorkspaceGrant(ROOT, ClientCapability.WRITE_TEXT_FILE, True)
    session = admission().admit(PRINCIPAL, value, chat_adapter_available=True, now=NOW)
    for obj, attr, changed in [
        (value, "workspace_root", "/other"),
        (grant, "standing", False),
        (session, "grants", (grant,)),
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(obj, attr, changed)
    assert session.grants == ()
    constructor: Any = type(session)
    with pytest.raises(TypeError):
        constructor(session.snapshot, value, grants=(grant,))


@pytest.mark.parametrize("root", ["", " "])
def test_empty_roots_refused(root: str) -> None:
    with pytest.raises(ValueError):
        CodingSessionRequest(root, frozenset())
    with pytest.raises(ValueError):
        WorkspaceGrant(root, ClientCapability.READ_TEXT_FILE, False)
    with pytest.raises(ValueError):
        CodingSessionAdmission(POLICY, trusted_roots=frozenset({root}))


def test_unknown_capabilities_refused() -> None:
    unknown: Any = "fs/delete"
    with pytest.raises(ValueError):
        CodingSessionRequest(ROOT, frozenset({unknown}))
    with pytest.raises(ValueError):
        WorkspaceGrant(ROOT, unknown, False)


def test_mutable_scope_input_cannot_change_admission() -> None:
    scopes: Any = {"chat:use"}
    principal = PrincipalContext("alice", "tenant", scopes)
    session = admission().admit(
        principal, request(), chat_adapter_available=True, now=NOW
    )
    scopes.add("admin")
    assert session.snapshot.principal.scopes == frozenset({"chat:use"})


@pytest.mark.parametrize("root", ["/workspace", ROOT + "/child", ROOT + "/../project"])
def test_trust_requires_exact_configured_root(root: str) -> None:
    with pytest.raises(AcpRefusal) as caught:
        admission().admit(
            PRINCIPAL,
            CodingSessionRequest(root, frozenset()),
            chat_adapter_available=True,
            now=NOW,
        )
    assert caught.value.reason is AcpRefusalReason.UNTRUSTED_ROOT


def test_read_reuses_session_binding_and_updates_only_activity() -> None:
    policy = admission()
    session = policy.admit(PRINCIPAL, request(), chat_adapter_available=True, now=NOW)
    checked = policy.check_capability(
        session,
        "fs/read_text_file",
        client_connected=True,
        chat_adapter_available=True,
        now=NOW + timedelta(minutes=1),
    )
    assert checked.snapshot.principal is PRINCIPAL
    assert checked.request is session.request
    assert checked.snapshot.absolute_expires_at == session.snapshot.absolute_expires_at
    assert checked.snapshot.last_activity_at == NOW + timedelta(minutes=1)
    assert session.snapshot.last_activity_at == NOW
    assert checked.grants == ()


@pytest.mark.parametrize(
    "method",
    [c.value for c in ClientCapability if c != ClientCapability.READ_TEXT_FILE],
)
def test_effects_and_permission_method_cannot_self_authorize(method: str) -> None:
    policy = admission()
    session = policy.admit(PRINCIPAL, request(), chat_adapter_available=True, now=NOW)
    with pytest.raises(AcpRefusal) as caught:
        policy.check_capability(
            session,
            method,
            client_connected=True,
            chat_adapter_available=True,
            now=NOW + timedelta(minutes=1),
        )
    assert caught.value.reason is AcpRefusalReason.PERMISSION_REQUIRED
    assert session.grants == ()
    assert session.snapshot.last_activity_at == NOW


def test_read_refused_when_root_is_no_longer_trusted() -> None:
    session = admission().admit(
        PRINCIPAL, request(), chat_adapter_available=True, now=NOW
    )
    changed_policy = CodingSessionAdmission(POLICY, trusted_roots=frozenset())
    with pytest.raises(AcpRefusal) as caught:
        changed_policy.check_capability(
            session,
            "fs/read_text_file",
            client_connected=True,
            chat_adapter_available=True,
            now=NOW + timedelta(minutes=1),
        )
    assert caught.value.reason is AcpRefusalReason.UNTRUSTED_ROOT
    assert session.snapshot.last_activity_at == NOW


@pytest.mark.parametrize(
    ("method", "connected", "available", "elapsed", "reason"),
    [
        ("fs/delete", True, True, 0, AcpRefusalReason.CAPABILITY_NOT_DECLARED),
        ("fs/read_text_file", False, True, 0, AcpRefusalReason.CLIENT_DISCONNECTED),
        (
            "fs/read_text_file",
            True,
            False,
            0,
            AcpRefusalReason.CHAT_ADAPTER_UNAVAILABLE,
        ),
        ("fs/read_text_file", True, True, 300, AcpRefusalReason.IDLE_EXPIRED),
        ("fs/read_text_file", True, True, 3600, AcpRefusalReason.ABSOLUTE_EXPIRED),
    ],
)
def test_request_refusals(
    method: str,
    connected: bool,
    available: bool,
    elapsed: int,
    reason: AcpRefusalReason,
) -> None:
    policy = admission()
    session = policy.admit(PRINCIPAL, request(), chat_adapter_available=True, now=NOW)
    with pytest.raises(AcpRefusal) as caught:
        policy.check_capability(
            session,
            method,
            client_connected=connected,
            chat_adapter_available=available,
            now=NOW + timedelta(seconds=elapsed),
        )
    assert caught.value.reason is reason
    assert session.snapshot.last_activity_at == NOW


def test_known_but_undeclared_read_is_refused() -> None:
    policy = admission()
    session = policy.admit(
        PRINCIPAL,
        CodingSessionRequest(ROOT, frozenset()),
        chat_adapter_available=True,
        now=NOW,
    )
    with pytest.raises(AcpRefusal) as caught:
        policy.check_capability(
            session,
            "fs/read_text_file",
            client_connected=True,
            chat_adapter_available=True,
            now=NOW,
        )
    assert caught.value.reason is AcpRefusalReason.CAPABILITY_NOT_DECLARED


def test_missing_principal_cannot_open_coding_session() -> None:
    with pytest.raises(AcpRefusal) as caught:
        admission().admit(None, request(), chat_adapter_available=True, now=NOW)
    assert caught.value.reason is AcpRefusalReason.UNAUTHENTICATED
