"""Controlled startup owners; these fixtures grant no deployed access."""

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import agent_utilities.graph
import pytest
import uvicorn

import arbora.agent_host as host
from arbora.agent_host.start_admission import (
    HostSessionBinding,
    HostStartAdmission,
    HostStartContext,
    HostStartGrant,
    HostStartRequest,
)
from arbora.agent_host.start_configuration import (
    HostRuntimeResources,
    HostStartConfiguration,
)
from arbora.api.contracts.session import GraphSession
from arbora.identity.engine import Resolution
from arbora.identity.ports import SessionState
from arbora.security.actor_context import ActorContext


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
        self.request = HostStartRequest(
            str(workspace),
            "terminal",
            False,
            HostStartConfiguration(
                workspace=str(workspace), enable_terminal_ui=True
            ).digest(),
        )
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
        self,
        state: SessionState,
        graph_session: GraphSession,
        *,
        runtime: HostRuntimeResources | None = None,
        configuration_digest: str | None = None,
    ) -> HostSessionBinding | None:
        return self.binding

    def context(self) -> HostStartContext:
        return HostStartContext(
            HostStartAdmission(self, self, self),
            self.request,
            self.graph,
            "exact-credential",
        )


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
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


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
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
    for name in ("create_graph_agent", "initialize_graph_from_workspace"):
        monkeypatch.setattr(agent_utilities.graph, name, effects)
    monkeypatch.setattr(uvicorn, "run", effects)
    with pytest.raises(PermissionError):
        host.create_agent_server(
            workspace=str(tmp_path), enable_terminal_ui=True, host_start=owner.context()
        )
    build.assert_not_called()
    effects.assert_not_called()


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
def test_matching_actor_fields_do_not_replace_owner_attestation(tmp_path: Path) -> None:
    owner = Owners(tmp_path)
    owner.binding = None
    with pytest.raises(PermissionError):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )
    assert owner.calls == 0


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
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


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
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


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
def test_attestation_for_equal_but_distinct_graph_is_refused(tmp_path: Path) -> None:
    owner = Owners(tmp_path)
    assert owner.binding is not None
    owner.binding = replace(owner.binding, graph_session=replace(owner.graph))
    with pytest.raises(PermissionError):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
@pytest.mark.parametrize("credential", ["", " ", "a b", "a\n"])
def test_malformed_credential_never_reaches_policy(
    tmp_path: Path, credential: str
) -> None:
    owner = Owners(tmp_path)
    context = replace(owner.context(), credential=credential)
    with pytest.raises(PermissionError):
        context.check(workspace=str(tmp_path), interfaces="terminal", web_logs=False)
    assert owner.calls == 0


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
def test_missing_graph_claim_is_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = Owners(tmp_path)
    monkeypatch.setattr(GraphSession, "engine_verified_context", lambda self: {})
    with pytest.raises(PermissionError, match="workspace_denied"):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
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


@pytest.mark.spec("ARBORA-HOST-R010.7.1")
def test_expired_session_never_calls_policy(tmp_path: Path) -> None:
    owner = Owners(tmp_path)
    owner.state = replace(owner.state, expires_at_ms=0)
    with pytest.raises(PermissionError):
        owner.context().check(
            workspace=str(tmp_path), interfaces="terminal", web_logs=False
        )
    assert owner.calls == 0


@pytest.mark.spec("ARBORA-HOST-R010.7.2.1")
@pytest.mark.parametrize(
    "field,value",
    [
        ("tag_prompts", {"code": "Assistant"}),
        ("tag_env_vars", {"code": "ENABLED"}),
        ("graph_name", "Other"),
        ("mcp_url", "http://127.0.0.1:1234"),
        ("mcp_config", "other.json"),
        ("router_model", "router"),
        ("agent_model", "agent"),
        ("min_confidence", 0.5),
        ("host", "0.0.0.0"),
        ("port", 9001),
        ("name", "Other"),
        ("system_prompt", "Other prompt"),
        ("enable_web_ui", True),
        ("enable_web_logs", True),
    ],
)
def test_actual_configuration_mismatch_precedes_all_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    owner = Owners(tmp_path)
    context = owner.context()
    forbidden = Mock(side_effect=AssertionError("owner or startup called"))
    monkeypatch.setattr(owner, "resolve_session", forbidden)
    monkeypatch.setattr(owner, "attest", forbidden)
    monkeypatch.setattr(owner, "authorize", forbidden)
    monkeypatch.setattr(host, "build_agent_host_app", forbidden)
    monkeypatch.setattr(uvicorn, "run", forbidden)
    with pytest.raises(PermissionError, match="configuration_mismatch"):
        host.create_agent_server(
            workspace=str(tmp_path),
            enable_terminal_ui=True,
            host_start=context,
            **{field: value},
        )
    forbidden.assert_not_called()


@pytest.mark.spec("ARBORA-HOST-R010.7.2.1")
@pytest.mark.parametrize(
    "field",
    ["graph", "graph_config", "executor", "host_logs", "host_log_runtime", "debug"],
)
def test_opaque_configuration_refuses_without_serializing_or_calling_owners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    class Opaque:
        def __repr__(self) -> str:
            raise AssertionError("serialized runtime object")

    owner = Owners(tmp_path)
    forbidden = Mock(side_effect=AssertionError("owner called"))
    monkeypatch.setattr(owner, "resolve_session", forbidden)
    arguments: dict[str, Any] = {field: Opaque()}
    with pytest.raises(PermissionError, match="configuration_unavailable"):
        host.create_agent_server(
            workspace=str(tmp_path),
            enable_terminal_ui=True,
            host_start=owner.context(),
            **arguments,
        )
    forbidden.assert_not_called()


@pytest.mark.spec("ARBORA-HOST-R010.7.2.1")
def test_canonical_configuration_mapping_order_and_workspace(tmp_path: Path) -> None:
    configuration = HostStartConfiguration(
        workspace=str(tmp_path), tag_prompts={"a": "one", "b": "two"}
    )
    assert (
        configuration.digest()
        == replace(
            configuration,
            workspace=str(tmp_path / "."),
            tag_prompts={"b": "two", "a": "one"},
        ).digest()
    )
    assert configuration.digest() != replace(configuration, port=9001).digest()


@pytest.mark.spec("ARBORA-HOST-R010.7.2.1")
@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
def test_noncanonical_configuration_refuses(value: Any) -> None:
    with pytest.raises(ValueError, match="invalid_launch_configuration"):
        HostStartConfiguration(min_confidence=value).digest()


@pytest.mark.spec("ARBORA-HOST-R010.7.2.1")
def test_matching_actual_inputs_reach_admission_but_never_launch(
    tmp_path: Path,
) -> None:
    owner = Owners(tmp_path)
    configuration = HostStartConfiguration(
        workspace=str(tmp_path),
        enable_terminal_ui=True,
        tag_prompts={"code": "Assistant"},
        port=9123,
    )
    owner.request = replace(owner.request, configuration_digest=configuration.digest())
    owner.grant = replace(owner.grant, request=owner.request)
    with pytest.raises(host.HostInterfaceUnavailable):
        host.create_agent_server(
            workspace=str(tmp_path),
            enable_terminal_ui=True,
            tag_prompts={"code": "Assistant"},
            port=9123,
            host_start=owner.context(),
        )
    assert owner.calls == 1
