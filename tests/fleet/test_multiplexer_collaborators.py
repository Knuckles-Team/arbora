"""Each multiplexer collaborator is built from data, never from a multiplexer.

ARBORA-HOST-R038.9: the multiplexer holds its collaborators as attributes built
in ``__init__``. A collaborator takes the shared state and the calls it needs as
keyword arguments, so it is constructed here with plain stand-ins and no
``MCPMultiplexer``, and it holds no reference to one.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.child_mounts import ChildMounts
from arbora.fleet.child_transport import ChildTransport
from arbora.fleet.fleet_probe import FleetProbe
from arbora.fleet.multiplexer import MCPMultiplexer
from arbora.fleet.proxied_calls import ProxiedCalls
from arbora.fleet.task_forwarding import TaskForwarding
from arbora.fleet.tool_discovery import ToolDiscovery


async def _no_publish(*_args: Any, **_kwargs: Any) -> None:
    return None


def _build_child_transport() -> ChildTransport:
    return ChildTransport(
        child_catalog_fingerprints={},
        child_policy_admitted_tools={},
        fleet=SimpleNamespace(),
        list_changed_tasks=set(),
        probe_cache={},
        publish_live_tools=_no_publish,
    )


def _build_task_forwarding() -> TaskForwarding:
    return TaskForwarding(
        fleet=SimpleNamespace(),
        signer=SimpleNamespace(),
        children={},
        catalog_epoch=lambda: 0,
        load_catalog=_no_publish,
        mount_child=_no_publish,
    )


def _build_proxied_calls() -> ProxiedCalls:
    return ProxiedCalls(
        child_policy_admitted_tools={},
        child_runtime_policies={},
        child_schema_refresh_errors={},
        child_schema_revisions={},
        fleet=SimpleNamespace(),
        host_mcp=lambda: None,
        children={},
        credential_authority=lambda: None,
        workflow_bindings=SimpleNamespace(),
        catalog=lambda: None,
        catalog_epoch=lambda: 0,
        aggregated_tools=lambda: {},
        tool_to_server=lambda: {},
        admit_runtime_policy_tools=_no_publish,
        open_one_session=_no_publish,
        load_catalog=_no_publish,
        notify_pending_tools_changed=_no_publish,
        tool_dispatchable=_no_publish,
    )


def _build_child_mounts() -> ChildMounts:
    stand_ins = {
        name: SimpleNamespace() for name in inspect.signature(ChildMounts).parameters
    }
    return ChildMounts(**stand_ins)


def _build_fleet_probe() -> FleetProbe:
    stand_ins = {
        name: SimpleNamespace() for name in inspect.signature(FleetProbe).parameters
    }
    return FleetProbe(**stand_ins)


def _build_tool_discovery() -> ToolDiscovery:
    stand_ins = {
        name: SimpleNamespace() for name in inspect.signature(ToolDiscovery).parameters
    }
    return ToolDiscovery(**stand_ins)


BUILDERS = {
    "transport": _build_child_transport,
    "tasks": _build_task_forwarding,
    "calls": _build_proxied_calls,
    "mounts": _build_child_mounts,
    "probe": _build_fleet_probe,
    "discovery": _build_tool_discovery,
}


@pytest.mark.spec("ARBORA-HOST-R038.9")
def test_each_collaborator_is_built_without_a_multiplexer() -> None:
    for build in BUILDERS.values():
        collaborator = build()
        parameters = inspect.signature(type(collaborator)).parameters
        assert not any("multiplexer" in name or name == "mux" for name in parameters)
        assert not any(
            isinstance(value, MCPMultiplexer) for value in vars(collaborator).values()
        )


@pytest.mark.spec("ARBORA-HOST-R038.9")
def test_the_multiplexer_holds_all_six_collaborators_built_in_init() -> None:
    assert len(BUILDERS) == 6
    init = inspect.getsource(MCPMultiplexer.__init__)
    for attribute, builder in BUILDERS.items():
        assert f"self.{attribute} = " in init
        assert type(builder()).__name__ in init


def _imported_modules(module: Any) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    froms = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    plain = [
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    ]
    return {name for name in froms if name} | set(plain)


@pytest.mark.spec("ARBORA-HOST-R038.9")
def test_no_collaborator_module_imports_the_multiplexer() -> None:
    for build in BUILDERS.values():
        module = inspect.getmodule(type(build()))
        assert "arbora.fleet.multiplexer" not in _imported_modules(module)
