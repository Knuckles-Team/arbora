"""Served coverage: the status block, the strict refusal, the named reasons.

ARBORA-FLEET-R060.3/060.4/060.5: ``fleet.status`` reports the coverage block
built from the ``workspace.yml`` registry, the enabled endpoints and the
catalog tool counts; with ``require_full_coverage`` the handler refuses
``UNAVAILABLE`` naming every offender; a missing or empty connector fails
the served status until it serves a tool.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.fleet import handle_fleet_operation, operations
from arbora.api.registry import Registry
from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.coverage import registered_connectors
from arbora.fleet.gateway_ops import FleetGateway, tool_for_multiplexer_ops
from arbora.fleet.multiplexer_ops import MultiplexerOps
from arbora.fleet.session_loads import SessionLoads


def _endpoint(name: str) -> Any:
    return SimpleNamespace(name=name)


def _manifest(connectors: list[Any]) -> dict[str, Any]:
    """The nested ``workspace.yml`` shape: the connectors group under subdirectories."""
    return {
        "subdirectories": {
            "arbora": {"subdirectories": {"connectors": {"repositories": connectors}}}
        }
    }


async def _harness(
    tmp_path,
    *,
    connectors,
    endpoints,
    unadmitted=(),
    tools_per_server,
    extra_rows=(),
) -> SimpleNamespace:
    path = tmp_path / "workspace.yml"
    path.write_text(yaml.safe_dump(_manifest(connectors)), encoding="utf-8")

    items: list[CatalogItem] = []
    for server, count in tools_per_server.items():
        items.extend(
            CatalogItem(f"fleet:tool:{server}/t{i}", "tool", f"t{i}", server=server)
            for i in range(count)
        )
    rows = (*items, *extra_rows)

    async def source():
        return rows

    async def allowed(_item, _caller, _extra=None):
        return True

    async def no_delegate(_op, _target, _params, _current):
        raise AssertionError("this test does not delegate")

    async def mount(item, call):
        return None

    async def notify(key):
        return True

    state = SimpleNamespace(
        endpoints=list(endpoints),
        unadmitted=list(unadmitted),
        rows=rows,
    )

    async def invoke(op, params, current, surface):
        raise AssertionError("this test does not call a child")

    ops = MultiplexerOps(
        catalog=FleetCatalog([source], allowed),
        sessions=SessionLoads(),
        loadable=allowed,
        mount=mount,
        notify=notify,
        invoke=invoke,
        health=lambda: {},
        coverage_registry=lambda: registered_connectors(path),
        coverage_endpoints=lambda: tuple(state.endpoints),
        unadmitted_servers=lambda: list(state.unadmitted),
    )
    gateway = FleetGateway(
        tool_for=tool_for_multiplexer_ops(ops),
        policy_check=allowed,
        delegated_call=no_delegate,
        catalog_ops=ops,
    )

    def caller():
        return SimpleNamespace(
            effective_scopes=frozenset({"mcp:discover", "mcp:delegate"}),
            tenant="tenant",
            principal="alice",
        )

    caller = caller()
    context = SimpleNamespace(caller=caller, services={"fleet_gateway": gateway})
    state.ops = ops
    state.context = context
    state.status_op = Registry(operations()).get("fleet.status")
    return state


async def _status(state, **params) -> Any:
    return await handle_fleet_operation(state.context, params, state.status_op)


CONNECTORS = [
    {"url": "https://github.com/Knuckles-Team/alpha.git", "mcp_server": "alpha"},
    {"url": "https://github.com/Knuckles-Team/beta.git", "mcp_server": "beta"},
    {"url": "https://github.com/Knuckles-Team/gamma.git", "mcp_server": "gamma"},
    {"url": "https://github.com/Knuckles-Team/delta.git", "mcp_server": "delta"},
]
HEALTHY_TOOLS = {"alpha": 2, "beta": 1, "gamma": 3}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R060.3")
async def test_status_reports_coverage_and_strict_mode_refuses(tmp_path) -> None:
    # every registered connector is served: the block is ok, strict passes
    state = await _harness(
        tmp_path,
        connectors=CONNECTORS,
        endpoints=[_endpoint(name) for name in ("alpha", "beta", "gamma", "delta")],
        tools_per_server={**HEALTHY_TOOLS, "delta": 1},
    )
    result = await _status(state)
    coverage = result["value"]["coverage"]
    assert coverage["ok"] is True
    assert coverage["registered"] == ["alpha", "beta", "delta", "gamma"]
    assert coverage["covered"] == ["alpha", "beta", "delta", "gamma"]
    assert coverage["missing"] == []
    assert coverage["zero_tools"] == []
    result = await _status(state, require_full_coverage=True)
    assert result["value"]["coverage"]["ok"] is True

    # one endpoint drops: non-strict still reports, strict refuses with
    # the stable code and the offender list
    state.endpoints.pop()
    result = await _status(state)
    assert result["value"]["coverage"]["ok"] is False
    with pytest.raises(OperationRefused) as refused:
        await _status(state, require_full_coverage=True)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == [
        {"server": "delta", "reason": "no_endpoint"}
    ]
    assert refused.value.details["zero_tools"] == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R060.4")
async def test_each_offender_reason_is_named(tmp_path) -> None:
    # four offenders: no endpoint, unadmitted, zero tools — each is named
    # with its closed-vocabulary reason; beta's catalog holds a prompt, so
    # it is admitted with zero tools rather than absent
    state = await _harness(
        tmp_path,
        connectors=CONNECTORS,
        endpoints=[_endpoint("alpha"), _endpoint("beta"), _endpoint("gamma")],
        unadmitted=("gamma",),
        tools_per_server={"alpha": 2, "gamma": 3},
        extra_rows=(
            CatalogItem("fleet:prompt:beta/guide", "prompt", "guide", server="beta"),
        ),
    )
    result = await _status(state)
    coverage = result["value"]["coverage"]
    assert coverage["ok"] is False
    assert coverage["missing"] == [
        {"server": "delta", "reason": "no_endpoint"},
        {"server": "gamma", "reason": "not_admitted"},
    ]
    assert coverage["zero_tools"] == ["beta"]
    # healthy servers are never listed
    assert coverage["covered"] == ["alpha"]

    with pytest.raises(OperationRefused) as refused:
        await _status(state, require_full_coverage=True)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == coverage["missing"]
    assert refused.value.details["zero_tools"] == coverage["zero_tools"]

    # the check never auto-disables, skips or hides an offender
    assert len(state.endpoints) == 3
    counts = await state.ops.catalog.tool_counts()
    assert counts == {"alpha": 2, "beta": 0, "gamma": 3}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R060", "ARBORA-FLEET-R060.5")
async def test_missing_and_empty_connectors_fail_status(tmp_path) -> None:
    # a fixture workspace.yml of four connectors; three with tools pass,
    # and the fourth is admitted with zero tools (a prompt, no tool)
    state = await _harness(
        tmp_path,
        connectors=CONNECTORS,
        endpoints=[_endpoint(name) for name in ("alpha", "beta", "gamma")],
        tools_per_server=HEALTHY_TOOLS,
        extra_rows=(
            CatalogItem("fleet:prompt:delta/guide", "prompt", "guide", server="delta"),
        ),
    )
    # the fourth is absent from the enabled endpoints
    with pytest.raises(OperationRefused) as refused:
        await _status(state, require_full_coverage=True)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == [
        {"server": "delta", "reason": "no_endpoint"}
    ]

    # then admitted with zero tools
    state.endpoints.append(_endpoint("delta"))
    with pytest.raises(OperationRefused) as refused:
        await _status(state, require_full_coverage=True)
    assert refused.value.details["missing"] == []
    assert refused.value.details["zero_tools"] == ["delta"]

    # after it serves one tool the call succeeds
    state.rows = (
        *state.rows,
        CatalogItem("fleet:tool:delta/t0", "tool", "t0", server="delta"),
    )

    async def source():
        return state.rows

    state.ops.catalog._sources = (source,)
    result = await _status(state, require_full_coverage=True)
    coverage = result["value"]["coverage"]
    assert coverage["ok"] is True
    assert coverage["covered"] == ["alpha", "beta", "delta", "gamma"]
