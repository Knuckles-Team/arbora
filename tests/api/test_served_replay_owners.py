"""The served registry reaches dispatch for plan runs and pack status (G2).

These tests compose the production registry (``get_registry()``) with the
invoke pipeline, as the served MCP door does, and assert that the call reaches
the operation runtime. A refusal at the durable-effect gate would mean nothing
dispatches (validation run 2026-10-24, gap G2).
"""

from __future__ import annotations

import pytest
from test_invoke_contract import caller, run, setup_services

from arbora.api.ops import get_registry
from arbora.api.registry import Effect, Surface
from arbora.mcp_server.served_invocation import validate_provider_schema

PLAN = {
    "tasks": [
        {"task_id": "gitlab", "prompt": "read the issue", "depends_on": []},
        {"task_id": "servicenow", "prompt": "open", "depends_on": ["gitlab"]},
    ],
    "output_task_id": "servicenow",
    "max_concurrency": 1,
    "max_steps": 2,
}


async def _no_fleet_effect(op, params, caller):
    raise AssertionError("these tests call no fleet tool")


def _served():
    registry = get_registry()
    # The production external-effect coverage (served_invocation.py).
    external = frozenset(
        op_id
        for op_id in ("fleet.call", "messaging.alert")
        if registry.get(op_id) is not None
    )
    services, runtime, _, journal, _ = setup_services(
        registry=registry,
        external_effect_operations=external,
        fleet_effect=_no_fleet_effect,
        schema_validate=validate_provider_schema,
    )

    async def dispatched():
        return {"value": {"dispatched": True}}

    runtime.on_dispatch = dispatched
    return services, runtime, journal


@pytest.mark.spec("ARBORA-RELEASE-R003.4.11")
def test_served_plan_run_reaches_dispatch_with_its_native_owner() -> None:
    services, runtime, journal = _served()
    assert "agents.plan_run" in services.native_idempotency
    supplied = caller(effective_scopes=frozenset({"kg:write"}))
    result = run(
        services,
        supplied=supplied,
        op_id="agents.plan_run",
        params={"plan": PLAN},
        surface=Surface.MCP,
        idempotency_key="dag-run-1",
    )
    assert result.code == "OK", result
    ((op, _, context),) = runtime.calls
    assert op.id == "agents.plan_run"
    assert context.idempotency_key == "dag-run-1"
    # The run itself reserves nothing: its step effects own replay.
    assert journal.rows == {}


@pytest.mark.spec("ARBORA-RELEASE-R003.4.11")
def test_served_plan_run_replay_binds_the_same_request_identity() -> None:
    services, runtime, _ = _served()
    supplied = caller(effective_scopes=frozenset({"kg:write"}))
    for _ in range(2):
        assert (
            run(
                services,
                supplied=supplied,
                op_id="agents.plan_run",
                params={"plan": PLAN},
                surface=Surface.MCP,
                idempotency_key="dag-run-1",
            ).code
            == "OK"
        )
    first, again = (context.caller.request_id for _, _, context in runtime.calls)
    assert first == again and first != supplied.request_id


@pytest.mark.spec("ARBORA-FLEET-R041.16")
def test_served_pack_status_is_a_read_and_reaches_dispatch_without_a_key() -> None:
    services, runtime, journal = _served()
    op = services.registry.get("fleet.packs.status")
    assert op is not None and op.effect is Effect.READ
    result = run(
        services,
        supplied=caller(effective_scopes=frozenset({"agent:pack-control"})),
        op_id="fleet.packs.status",
        params={"connector": "gitlab-mcp"},
        surface=Surface.MCP,
    )
    assert result.code == "OK", result
    assert [call[0].id for call in runtime.calls] == ["fleet.packs.status"]
    assert journal.rows == {}


@pytest.mark.spec("ARBORA-FLEET-R041.16")
def test_pack_status_tenant_comes_from_the_caller_not_the_request() -> None:
    services, runtime, _ = _served()
    result = run(
        services,
        supplied=caller(effective_scopes=frozenset({"agent:pack-control"})),
        op_id="fleet.packs.status",
        params={"connector": "gitlab-mcp", "tenant_id": "other"},
        surface=Surface.MCP,
    )
    assert result.code == "INVALID_ARGUMENT"
    assert runtime.calls == []
