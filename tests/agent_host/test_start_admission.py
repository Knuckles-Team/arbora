"""Controlled startup owners; these fixtures grant no deployed access."""

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import agent_utilities
import pytest
import uvicorn
from agent_utilities.api.session import GraphSession
from agent_utilities.security.brain_context import ActorContext

import graph_os.agent_host as host
from graph_os.agent_host.start_admission import (
    HostSessionBinding,
    HostStartAdmission,
    HostStartContext,
    HostStartGrant,
    HostStartRequest,
)
from graph_os.identity.engine import Resolution
from graph_os.identity.ports import SessionState


class Owners:
    def __init__(self, workspace: Path) -> None:
        self.graph = GraphSession(
            actor=ActorContext(
                actor_id="alice", tenant_id="tenant", authenticated=True
            ),
            tenant="tenant",
            audience="host",
            policy_version="policy-1",
        )
        resolution = Resolution(
            "alice",
            "alice",
            "human",
            "active",
            False,
            (),
            (),
            (),
            False,
            False,
            False,
            self.graph.engine_verified_context(),
        )
        self.state = SessionState(
            resolution, int(time.time() * 1000) + 60_000, "session-alice", None
        )
        self.request = HostStartRequest(str(workspace), "terminal", False, "a" * 64)
        self.grant = HostStartGrant(
            self.request,
            "tenant",
            "alice",
            "session-alice",
            "policy-1",
            "grant-1",
            self.state.expires_at_ms,
            "host.start",
        )
        self.binding: HostSessionBinding | None = HostSessionBinding(
            self.state, self.graph, self.state.expires_at_ms
        )
        self.revoke = False
        self.calls = 0

    async def resolve_session(self, credential: str) -> SessionState:
        if credential != "exact-credential" or self.revoke:
            raise PermissionError("denied")
        return self.state

    async def authorize(
        self, state: SessionState, request: HostStartRequest
    ) -> HostStartGrant:
        self.calls += 1
        return self.grant

    async def attest(
        self, state: SessionState, graph_session: GraphSession
    ) -> HostSessionBinding | None:
        return self.binding

    def context(self) -> HostStartContext:
        return HostStartContext(
            HostStartAdmission(self, self, self),
            self.request,
            self.graph,
            "exact-credential",
        )


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
def test_matching_owner_attestation_admits_exact_graph_session(tmp_path: Path) -> None:
    owner = Owners(tmp_path)
    context = owner.context()
    admitted = context.check(
        workspace=str(tmp_path), interfaces="terminal", web_logs=False
    )
    assert admitted.graph_session is owner.graph
    assert admitted.state is owner.state
    assert admitted.grant.action == "host.start"
    assert "exact-credential" not in repr(context)


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant", "foreign"),
        ("principal", "foreign"),
        ("session_ref", "foreign"),
        ("policy_revision", "foreign"),
        ("action", "host.logs.read"),
        ("expires_at_ms", 0),
        ("revision", ""),
    ],
)
def test_grant_mismatch_refuses_before_host_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    owner = Owners(tmp_path)
    owner.grant = replace(owner.grant, **{field: value})
    build = Mock(side_effect=AssertionError("host built"))
    monkeypatch.setattr(host, "build_agent_host_app", build)
    effects = Mock(side_effect=AssertionError("startup effect"))
    for name in (
        "initialize_workspace",
        "create_graph_agent",
        "initialize_graph_from_workspace",
    ):
        monkeypatch.setattr(agent_utilities, name, effects)
    monkeypatch.setattr(uvicorn, "run", effects)
    with pytest.raises(PermissionError):
        host.create_agent_server(
            workspace=str(tmp_path), enable_terminal_ui=True, host_start=owner.context()
        )
    build.assert_not_called()
    effects.assert_not_called()


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
def test_matching_actor_fields_do_not_replace_owner_attestation(tmp_path: Path) -> None:
    owner = Owners(tmp_path)
    owner.binding = None
    with pytest.raises(PermissionError):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )
    assert owner.calls == 0


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
def test_admission_does_not_enable_missing_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = Owners(tmp_path)
    build = Mock(side_effect=AssertionError("host built"))
    monkeypatch.setattr(host, "build_agent_host_app", build)
    with pytest.raises(host.HostInterfaceUnavailable):
        host.create_agent_server(
            workspace=str(tmp_path), enable_terminal_ui=True, host_start=owner.context()
        )
    assert owner.calls == 1
    build.assert_not_called()


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
def test_revocation_during_policy_lookup_refuses(tmp_path: Path) -> None:
    owner = Owners(tmp_path)

    class RevokingPolicy:
        async def authorize(
            self, state: SessionState, request: HostStartRequest
        ) -> HostStartGrant:
            owner.revoke = True
            return owner.grant

    admission = HostStartAdmission(owner, RevokingPolicy(), owner)
    with pytest.raises(PermissionError):
        asyncio.run(admission.admit("exact-credential", owner.request, owner.graph))


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
def test_attestation_for_equal_but_distinct_graph_is_refused(tmp_path: Path) -> None:
    owner = Owners(tmp_path)
    assert owner.binding is not None
    owner.binding = replace(owner.binding, graph_session=replace(owner.graph))
    with pytest.raises(PermissionError):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
@pytest.mark.parametrize("credential", ["", " ", "a b", "a\n"])
def test_malformed_credential_never_reaches_policy(
    tmp_path: Path, credential: str
) -> None:
    owner = Owners(tmp_path)
    context = replace(owner.context(), credential=credential)
    with pytest.raises(PermissionError):
        context.check(workspace=str(tmp_path), interfaces="terminal", web_logs=False)
    assert owner.calls == 0


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
def test_missing_graph_claim_is_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = Owners(tmp_path)
    monkeypatch.setattr(GraphSession, "engine_verified_context", lambda self: {})
    with pytest.raises(PermissionError, match="workspace_denied"):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
@pytest.mark.parametrize(
    "changes",
    [{"interfaces": "both"}, {"web_logs": True}, {"configuration_digest": "b" * 64}],
)
def test_policy_request_mismatch_refuses(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    owner = Owners(tmp_path)
    owner.grant = replace(owner.grant, request=replace(owner.request, **changes))
    with pytest.raises(PermissionError):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )


@pytest.mark.spec("GRAPHOS-HOST-R010.7.1")
def test_expired_session_never_calls_policy(tmp_path: Path) -> None:
    owner = Owners(tmp_path)
    owner.state = replace(owner.state, expires_at_ms=0)
    with pytest.raises(PermissionError):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )
    assert owner.calls == 0
