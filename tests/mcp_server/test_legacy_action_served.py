"""ARBORA-HOST-R005.2.9: legacy actions resolve at the MCP dispatch."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from pydantic import BaseModel, ConfigDict

from arbora.api.errors import ArboraErrorCode, ArboraRefusal
from arbora.api.invoke import InvokeServices, invoke
from arbora.api.invoke.plan import EgPlanStore
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.mcp.verbs import MCPProjection
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    Registry,
    Surface,
    Verb,
)
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.tools import analysis_tools, write_ingest_tools
from arbora.mcp_server.legacy_action_invoke import invoke_legacy_action
from arbora.mcp_server.legacy_action_mapping import (
    APPROVED_ACTION_MAPPING,
    ActionOperationMapping,
    require_served_action,
)
from tests.api._support import make_verified_caller
from tests.api.test_mcp_serving import (
    _FixtureJournal,
    _FixtureLeases,
    _FixtureRuntime,
)


def _text(result) -> str:
    return result.content[0].text


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = ""
    corpus_name: str = ""
    kind: str = ""
    target_path: str = ""


class _Runtime(_FixtureRuntime):
    async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
        self.calls.append((op.id, dict(params)))
        return {"target": params.get("target", ""), "corpus_name": ""}


def _operation(op_id: str, effect: Effect = Effect.READ) -> OpSpec:
    return OpSpec(
        id=op_id,
        verb=Verb.ASK,
        summary="Fixture mapped operation",
        examples=("fixture mapped operation",),
        params=_Params,
        result=_Params,
        binding=Composite(handler="fixture.operation"),
        scopes=frozenset({"example:ask"}),
        effect=effect,
        audit=AuditClass.EVENT,
        idempotency=Idempotency.NONE,
        surfaces=frozenset(Surface),
    )


def _serve(monkeypatch, *, authenticated: bool = True):
    """Compose a real ``invoke`` pipeline behind the served projection."""
    runtime_double = _Runtime()
    audit: list[tuple[str, str]] = []

    async def preflight(event: Any, _cls: Any, supplied: Any) -> str:
        audit.append(("preflight", event["op"]))
        return "receipt:" + supplied.request_id

    async def write(event: Any, _cls: Any, _supplied: Any) -> None:
        audit.append(("write", event["op"]))

    registry = Registry(
        [
            _operation("ingest.sources.list"),
            _operation("ontology.inspect"),
            _operation("ingest.submissions.create", Effect.WRITE),
        ]
    )
    services = InvokeServices(
        registry=registry,
        runtime=runtime_double,
        plans=EgPlanStore(
            SimpleNamespace(control_leases=_FixtureLeases()),
            lease_kind="fixture.plan",
            seal_key=b"x" * 32,
        ),
        policy_gate=PolicyGate("none"),
        audit_preflight=preflight,
        audit_write=write,
        effects=_FixtureJournal(),
        external_effect_operations=frozenset({"ingest.submissions.create"}),
    )
    caller = make_verified_caller("example:ask")
    projection = MCPProjection(
        registry,
        services,
        IntentResolver(),
        lambda: caller(authenticated=authenticated),
        PolicyGate("none"),
        invoke,
    )
    monkeypatch.setattr(runtime, "SERVED_PROJECTION", projection)
    return runtime_double, audit


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
async def test_mapped_and_retired_actions_over_mcp(monkeypatch) -> None:
    served, audit = _serve(monkeypatch)
    server = FastMCP("legacy-action-served")
    write_ingest_tools.register_write_ingest_tools(server, lambda: None)
    async with Client(server) as client:
        mapped = await client.call_tool(
            "graph_ingest",
            {"action": "corpus", "corpus_name": "docs"},
            raise_on_error=False,
        )
        retired = await client.call_tool(
            "graph_ingest", {"action": "clear"}, raise_on_error=False
        )
    # A mapped read runs through invoke as the verified caller.
    envelope = json.loads(_text(mapped))
    assert envelope["ok"] is True, envelope
    assert served.calls == [
        (
            "ingest.sources.list",
            {"target": "", "corpus_name": "docs", "kind": "", "target_path": ""},
        )
    ]
    # A mapped write is audited: the record is reserved, then written.
    async with Client(server) as client:
        written = await client.call_tool(
            "graph_ingest",
            {"action": "ingest", "target_path": "/repo"},
            raise_on_error=False,
        )
    assert json.loads(_text(written))["ok"] is True, _text(written)
    assert served.calls[1][0] == "ingest.submissions.create"
    assert served.calls[1][1]["kind"] == "ingest"
    assert served.calls[1][1]["target_path"] == "/repo"
    assert audit == [
        ("preflight", "ingest.submissions.create"),
        ("write", "ingest.submissions.create"),
    ]
    # A retired action is refused at the gate, before any operation runs.
    body = json.loads(_text(retired))
    assert body["status"] == "failed"
    assert body["error_class"] == "ArboraRefusal"
    assert len(served.calls) == 2


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
async def test_analysis_action_runs_through_invoke_as_the_caller(monkeypatch) -> None:
    served, audit = _serve(monkeypatch)
    server = FastMCP("legacy-analysis-served")
    analysis_tools.register_analysis_tools(server)
    bundle = await analysis_tools.execute_focused_analysis("inspect", target="node-1")
    assert served.calls == [
        (
            "ontology.inspect",
            {"target": "node-1", "corpus_name": "", "kind": "", "target_path": ""},
        )
    ]
    assert bundle is not None


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
async def test_an_unverified_caller_reaches_no_operation(monkeypatch) -> None:
    served, audit = _serve(monkeypatch, authenticated=False)
    server = FastMCP("legacy-action-unauthenticated")
    write_ingest_tools.register_write_ingest_tools(server, lambda: None)
    async with Client(server) as client:
        refused = await client.call_tool(
            "graph_ingest", {"action": "corpus"}, raise_on_error=False
        )
    body = json.loads(_text(refused))
    assert body["status"] == "failed"
    assert served.calls == []
    assert audit == []


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
async def test_no_served_projection_refuses_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "SERVED_PROJECTION", None)
    with pytest.raises(ArboraRefusal) as refused:
        await invoke_legacy_action("corpus", {})
    assert refused.value.code == ArboraErrorCode.UNAVAILABLE


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
def test_the_retired_handlers_are_deleted() -> None:
    assert not hasattr(write_ingest_tools, "_INGEST_ACTION_DISPATCH")
    assert not hasattr(analysis_tools, "_ANALYSIS_ACTION_DISPATCH")
    assert not hasattr(analysis_tools, "_analysis_action_inspect")
    assert not hasattr(write_ingest_tools, "_ingest_action_corpus")


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
def test_retired_action_answers_unavailable_naming_the_owner_row() -> None:
    with pytest.raises(ArboraRefusal) as retired:
        require_served_action("clear")
    assert retired.value.code == ArboraErrorCode.UNAVAILABLE
    assert retired.value.details["spec_row"] == "MEDULLA-INGEST-WORKER"
    assert require_served_action("corpus").operation == "ingest.sources.list"


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
def test_unknown_action_is_an_invalid_argument() -> None:
    with pytest.raises(ArboraRefusal) as unknown:
        require_served_action("an_action_the_retired_host_never_had")
    assert unknown.value.code == ArboraErrorCode.INVALID_ARGUMENT


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
def test_an_entry_names_exactly_one_of_operation_and_retired_by() -> None:
    with pytest.raises(ValueError):
        ActionOperationMapping("a")
    with pytest.raises(ValueError):
        ActionOperationMapping("a", "query.uql", "MEDULLA-DECISION-ENGINE")
    assert APPROVED_ACTION_MAPPING["clear"].retired_by == "MEDULLA-INGEST-WORKER"


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
def test_the_unwired_validator_wrapper_is_gone() -> None:
    assert not hasattr(runtime, "served_legacy_actions_within_approved_mapping")
