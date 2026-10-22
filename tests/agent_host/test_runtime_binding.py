"""Controlled construction owner facts; no production authority is minted."""
# spec: ARBORA-HOST-R010.7.2.2.1

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from arbora.agent_host.start_admission import HostSessionBinding
from arbora.agent_host.start_configuration import (
    HostRuntimeResources,
    HostStartConfiguration,
)
from tests.agent_host.test_start_admission import Owners


class ConstructionOwner(Owners):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.configuration = HostStartConfiguration(
            workspace=str(workspace),
            enable_terminal_ui=True,
            tag_prompts={"code": "original"},
        )
        self.request = replace(
            self.request, configuration_digest=self.configuration.digest()
        )
        self.grant = replace(self.grant, request=self.request)
        self.resources = HostRuntimeResources(object(), object(), {"engine": object()})
        self.binding = HostSessionBinding(
            self.state,
            self.graph,
            self.state.expires_at_ms,
            self.resources,
            self.request.configuration_digest,
            "owner-revision-1",
        )
        self.attestations = 0
        self.change: dict[str, Any] = {}
        self.mutate_configuration = False

    async def attest(
        self, state, graph_session, *, runtime=None, configuration_digest=None
    ):
        assert state == self.state and graph_session is self.graph
        assert runtime is self.resources
        assert configuration_digest == self.request.configuration_digest
        self.attestations += 1
        assert self.binding is not None
        if self.attestations > 1:
            return replace(self.binding, **self.change)
        return self.binding

    async def authorize(self, state, request):
        if self.mutate_configuration:
            assert self.configuration.tag_prompts is not None
            self.configuration.tag_prompts["code"] = "changed while awaiting owner"
        return await super().authorize(state, request)


def test_exact_borrowed_runtime_is_returned_only_after_owner_admission(
    tmp_path: Path,
) -> None:
    owner = ConstructionOwner(tmp_path)
    checked = owner.context().check_runtime(owner.configuration, owner.resources)
    assert checked.runtime is owner.resources
    assert checked.runtime.graph_config is owner.resources.graph_config
    assert checked.configuration_revision == "owner-revision-1"
    assert checked.graph_session is owner.graph
    assert owner.attestations == 3
    assert owner.calls == 1


@pytest.mark.parametrize(
    "field",
    ["graph", "engine", "graph_config", "executor", "host_logs", "host_log_runtime"],
)
def test_replaced_resource_is_denied_even_with_equal_configuration(
    tmp_path: Path, field: str
) -> None:
    owner = ConstructionOwner(tmp_path)
    owner.change["runtime"] = replace(owner.resources, **{field: object()})
    with pytest.raises(PermissionError):
        owner.context().check_runtime(owner.configuration, owner.resources)


@pytest.mark.parametrize(
    "change",
    [
        {"runtime": None},
        {"configuration_revision": None},
        {"configuration_revision": ""},
        {"configuration_revision": "owner-revision-2"},
        {"configuration_digest": "b" * 64},
        {"expires_at_ms": 0},
    ],
)
def test_absent_or_changed_owner_binding_denies(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    owner = ConstructionOwner(tmp_path)
    owner.change = change
    with pytest.raises(PermissionError):
        owner.context().check_runtime(owner.configuration, owner.resources)


def test_actual_configuration_change_precedes_owner_calls(tmp_path: Path) -> None:
    owner = ConstructionOwner(tmp_path)
    with pytest.raises(PermissionError, match="configuration_mismatch"):
        owner.context().check_runtime(
            replace(owner.configuration, port=9001), owner.resources
        )
    assert owner.calls == owner.attestations == 0


def test_declarative_mutation_during_owner_await_refuses(tmp_path: Path) -> None:
    owner = ConstructionOwner(tmp_path)
    owner.mutate_configuration = True
    with pytest.raises(PermissionError, match="configuration_mismatch"):
        owner.context().check_runtime(owner.configuration, owner.resources)


def test_owner_must_explicitly_attest_runtime_not_only_session(tmp_path: Path) -> None:
    owner = ConstructionOwner(tmp_path)
    owner.binding = HostSessionBinding(
        owner.state, owner.graph, owner.state.expires_at_ms
    )
    with pytest.raises(PermissionError, match="configuration_unavailable"):
        owner.context().check_runtime(owner.configuration, owner.resources)
    assert owner.calls == 0


def test_identity_equality_does_not_invoke_resource_comparison(tmp_path: Path) -> None:
    class Opaque:
        def __eq__(self, other: object) -> bool:
            raise AssertionError("runtime equality must not mint authority")

    owner = ConstructionOwner(tmp_path)
    owner.resources = replace(owner.resources, engine=Opaque())
    assert owner.binding is not None
    owner.binding = replace(owner.binding, runtime=owner.resources)
    owner.change["runtime"] = replace(owner.resources, engine=Opaque())
    with pytest.raises(PermissionError):
        owner.context().check_runtime(owner.configuration, owner.resources)


def test_runtime_session_revocation_still_denies(tmp_path: Path) -> None:
    owner = ConstructionOwner(tmp_path)
    owner.revoke = True
    with pytest.raises(PermissionError):
        owner.context().check_runtime(owner.configuration, owner.resources)
    assert owner.attestations == owner.calls == 0


def test_runtime_session_revocation_during_policy_await_denies(tmp_path: Path) -> None:
    class RevokingOwner(ConstructionOwner):
        async def authorize(self, state, request):
            self.revoke = True
            return await super().authorize(state, request)

    owner = RevokingOwner(tmp_path)
    with pytest.raises(PermissionError):
        owner.context().check_runtime(owner.configuration, owner.resources)
    assert owner.calls == 1


def test_runtime_request_must_match_actual_interface_and_workspace(
    tmp_path: Path,
) -> None:
    owner = ConstructionOwner(tmp_path)
    owner.request = replace(owner.request, interfaces="web")
    with pytest.raises(PermissionError, match="configuration_mismatch"):
        owner.context().check_runtime(owner.configuration, owner.resources)
    assert owner.calls == owner.attestations == 0


def test_final_session_refresh_cannot_hide_changed_runtime_revision(
    tmp_path: Path,
) -> None:
    class ChangingOwner(ConstructionOwner):
        resolutions = 0

        async def resolve_session(self, credential):
            self.resolutions += 1
            if self.resolutions == 2:
                self.change["configuration_revision"] = "changed-during-session-refresh"
            return await super().resolve_session(credential)

    owner = ChangingOwner(tmp_path)
    with pytest.raises(PermissionError):
        owner.context().check_runtime(owner.configuration, owner.resources)
    assert owner.attestations == 3


@pytest.mark.parametrize("delta", [1000, -1000])
def test_idle_expiry_refresh_preserves_original_admission_ceiling(
    tmp_path: Path, delta: int
) -> None:
    class RefreshingOwner(ConstructionOwner):
        resolutions = 0

        async def resolve_session(self, credential):
            self.resolutions += 1
            state = await super().resolve_session(credential)
            if self.resolutions == 2:
                assert self.binding is not None
                refreshed = replace(state, expires_at_ms=state.expires_at_ms + delta)
                self.change["state"] = refreshed
                return refreshed
            return state

    owner = RefreshingOwner(tmp_path)
    if delta < 0:
        with pytest.raises(PermissionError):
            owner.context().check_runtime(owner.configuration, owner.resources)
    else:
        checked = owner.context().check_runtime(owner.configuration, owner.resources)
        assert checked.expires_at_ms == owner.state.expires_at_ms
        assert checked.state is owner.state


@pytest.mark.parametrize(
    "field,value", [("session_ref", "another-session"), ("mfa_at_ms", 123)]
)
def test_final_session_refresh_cannot_change_authority(
    tmp_path: Path, field: str, value: Any
) -> None:
    class ChangingOwner(ConstructionOwner):
        resolutions = 0

        async def resolve_session(self, credential):
            self.resolutions += 1
            state = await super().resolve_session(credential)
            return replace(state, **{field: value}) if self.resolutions == 2 else state

    owner = ChangingOwner(tmp_path)
    with pytest.raises(PermissionError):
        owner.context().check_runtime(owner.configuration, owner.resources)


def launch_supplied(owner, **overrides):
    import arbora.agent_host as host

    arguments = {
        "workspace": owner.configuration.workspace,
        "tag_prompts": owner.configuration.tag_prompts,
        "enable_terminal_ui": True,
        "host_start": owner.context(),
        "host_runtime": owner.resources,
    }
    arguments.update(overrides)
    host.create_agent_server(**arguments)


def test_public_launcher_revalidates_bundle_without_starting(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import uvicorn

    import arbora.agent_host as host

    owner = ConstructionOwner(tmp_path)
    forbidden = Mock(side_effect=AssertionError("must not construct or listen"))
    monkeypatch.setattr(host, "build_agent_host_app", forbidden)
    monkeypatch.setattr(uvicorn, "run", forbidden)
    with pytest.raises(host.HostInterfaceUnavailable):
        launch_supplied(owner, graph=owner.resources.graph)
    assert owner.attestations == 3
    assert owner.calls == 1
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "field",
    ["graph", "graph_config", "executor", "host_logs", "host_log_runtime", "debug"],
)
def test_public_launcher_refuses_conflicting_alias_before_owner(tmp_path, field):
    owner = ConstructionOwner(tmp_path)
    with pytest.raises(PermissionError, match="configuration_unavailable"):
        launch_supplied(owner, **{field: object()})
    assert owner.attestations == owner.calls == 0


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"configuration_revision": "changed"}, "workspace_denied"),
        ({"runtime": None}, "configuration_unavailable"),
    ],
)
def test_public_launcher_refuses_changed_owner_binding(tmp_path, change, reason):
    owner = ConstructionOwner(tmp_path)
    owner.change = change
    with pytest.raises(PermissionError, match=reason):
        launch_supplied(owner)


def test_public_launcher_never_treats_bundle_as_authority(tmp_path):
    owner = ConstructionOwner(tmp_path)
    with pytest.raises(PermissionError, match="configuration_unavailable"):
        launch_supplied(owner, host_start=None)
    assert owner.attestations == owner.calls == 0


def test_public_launcher_rejects_bundle_on_legacy_path(tmp_path):
    owner = ConstructionOwner(tmp_path)
    with pytest.raises(ValueError, match="runtime_requires_selected_interface"):
        launch_supplied(owner, enable_terminal_ui=False)
    assert owner.attestations == owner.calls == 0
