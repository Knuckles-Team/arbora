"""A plan-confirmed destructive relay answers the connector's confirmation (G15).

ARBORA-FLEET-R015.2. The child is a real FastMCP server process whose delete
tool runs the connector SDK's own guard, ``ctx_confirm_destructive``, as the
ServiceNow connector does. Arbora is its MCP client.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet.plan_confirmation import confirmed_plan_call
from tests.fleet.catalog_fixture import multiplexer_from_fixture

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015.2")

TOOL = "servicenow_delete_table_record"

_CHILD = """
import sys
from fastmcp import Context, FastMCP
from scion_sdk.mcp.confirmation import ctx_confirm_destructive

deleted = []
mcp = FastMCP("sn-child")


@mcp.tool(name="servicenow_delete_table_record", annotations={"destructiveHint": True})
async def delete(sys_id: str, ctx: Context) -> dict:
    confirmed = await ctx_confirm_destructive(
        ctx, "delete_table_record", confirmation_arguments={"sys_id": sys_id}
    )
    if confirmed is not True:
        return {"cancelled": True, "operation": "delete_table_record"}
    deleted.append(sys_id)
    return {"deleted": sys_id, "count": len(deleted)}


mcp.run(transport="http", host="127.0.0.1", port=int(sys.argv[1]), path="/mcp",
        show_banner=False, log_level="error")
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def child_port():
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-c", _CHILD, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.1)
    yield port
    process.kill()
    process.wait(timeout=10)


async def _runtime(tmp_path, port: int) -> Any:
    mux = multiplexer_from_fixture(tmp_path / "c.json")
    started = await mux._start_child(
        "sn-child",
        {
            "url": f"http://127.0.0.1:{port}/mcp",
            "connect_timeout": 20,
            "allowed_private_hosts": ["127.0.0.1"],
        },
    )
    assert started is not None
    return started[1]


def _payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if isinstance(structured, dict):
        return structured.get("result", structured)
    import json

    return json.loads(result.content[0].text)


async def test_a_plan_confirmed_delete_is_confirmed_and_runs(tmp_path, child_port):
    runtime = await _runtime(tmp_path, child_port)
    try:
        with confirmed_plan_call(TOOL):
            result = await runtime.call_tool(TOOL, {"sys_id": "abc"})
        assert _payload(result) == {"deleted": "abc", "count": 1}
    finally:
        await runtime.aclose()


async def test_an_unconfirmed_delete_is_refused_by_the_connector(tmp_path, child_port):
    runtime = await _runtime(tmp_path, child_port)
    try:
        result = await runtime.call_tool(TOOL, {"sys_id": "abc"})
        assert _payload(result)["cancelled"] is True
        # A plan that confirmed another tool does not confirm this one.
        with confirmed_plan_call("servicenow_delete_incident"):
            result = await runtime.call_tool(TOOL, {"sys_id": "abc"})
        assert _payload(result)["cancelled"] is True
    finally:
        await runtime.aclose()


def _pipeline():
    from arbora.api.invoke import FleetCallDecision
    from arbora.api.ops import get_registry
    from arbora.api.ops.fleet import _plan_confirmed
    from arbora.api.registry import Confirm, Effect, Executor, PrincipalRule
    from arbora.mcp_server.served_invocation import validate_provider_schema
    from tests.api.test_invoke_contract import setup_services

    async def destructive(op, params, supplied):
        return FleetCallDecision(
            effect=Effect.DESTRUCTIVE,
            confirm=Confirm.PLAN,
            principals=PrincipalRule.ANY,
            executor=Executor.CALLER,
            required_scopes=frozenset(),
            executor_scopes=frozenset(),
            subject_id=None,
            credential_mode="delegated",
        )

    registry = get_registry()
    services, runtime, _, _, leases = setup_services(
        registry=registry,
        external_effect_operations=frozenset({"fleet.call"}),
        fleet_effect=destructive,
        schema_validate=validate_provider_schema,
    )
    marked: list[bool] = []

    async def dispatch(op, params, context):
        marked.append(_plan_confirmed(context, op))
        return {"value": {"deleted": params["arguments"]["sys_id"]}}

    runtime.dispatch = dispatch
    return services, marked, leases


def _call(services, **kwargs):
    from arbora.api.registry import Surface
    from tests.api.test_invoke_contract import caller, run

    params = kwargs.pop(
        "params", {"server": "sn-child", "tool": TOOL, "arguments": {"sys_id": "abc"}}
    )
    return run(
        services,
        supplied=caller(effective_scopes=frozenset({"mcp:delegate"})),
        op_id="fleet.call",
        params=params,
        surface=Surface.MCP,
        **kwargs,
    )


def test_only_a_consumed_plan_marks_the_destructive_dispatch() -> None:
    services, marked, _ = _pipeline()
    preview = _call(services)
    assert preview.code == "CONFIRMATION_REQUIRED" and marked == []
    assert _call(services, plan_ref=preview.details["plan_ref"]).code == "OK"
    assert marked == [True]


@pytest.mark.parametrize("change", ["expired", "mismatched", "unknown"])
def test_an_expired_or_mismatched_plan_dispatches_nothing(change: str) -> None:
    services, marked, leases = _pipeline()
    reference = _call(services).details["plan_ref"]
    kwargs: dict[str, Any] = {"plan_ref": reference}
    if change == "expired":
        leases.rows[reference]["hard_expires_at_ms"] = 0
    elif change == "mismatched":
        kwargs["params"] = {
            "server": "sn-child",
            "tool": TOOL,
            "arguments": {"sys_id": "other"},
        }
    else:
        kwargs["plan_ref"] = "plan-that-does-not-exist"
    assert _call(services, **kwargs).code != "OK"
    assert marked == []


def test_a_non_destructive_or_keyed_call_is_never_marked() -> None:
    from arbora.api.ops.fleet import _plan_confirmed
    from arbora.api.registry import Effect

    write: Any = SimpleNamespace(effect=Effect.WRITE)
    destructive: Any = SimpleNamespace(effect=Effect.DESTRUCTIVE)
    assert not _plan_confirmed(SimpleNamespace(idempotency_key="plan:x"), write)
    assert not _plan_confirmed(SimpleNamespace(idempotency_key="k"), destructive)
    assert _plan_confirmed(SimpleNamespace(idempotency_key="plan:x"), destructive)
