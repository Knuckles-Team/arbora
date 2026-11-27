"""ARBORA-HOST-R005.2.9: legacy actions resolve at the MCP dispatch."""

from __future__ import annotations

import json

import pytest
from fastmcp import Client, FastMCP

from arbora.api.errors import ArboraErrorCode, ArboraRefusal
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.tools import write_ingest_tools
from arbora.mcp_server.legacy_action_mapping import (
    APPROVED_ACTION_MAPPING,
    ActionOperationMapping,
    require_served_action,
)


def _text(result) -> str:
    return result.content[0].text


@pytest.mark.spec("ARBORA-HOST-R005.2.9")
async def test_mapped_and_retired_actions_over_mcp(monkeypatch) -> None:
    monkeypatch.setattr(runtime, "_get_engine", lambda: object())
    server = FastMCP("legacy-action-served")
    write_ingest_tools.register_write_ingest_tools(server)
    async with Client(server) as client:
        mapped = await client.call_tool(
            "graph_ingest", {"action": "corpus"}, raise_on_error=False
        )
        retired = await client.call_tool(
            "graph_ingest", {"action": "clear"}, raise_on_error=False
        )
    # A mapped action reaches its handler; a retired one is refused at the gate.
    assert _text(mapped) == "Error: corpus_name required"
    body = json.loads(_text(retired))
    assert body["status"] == "failed"
    assert body["error_class"] == "ArboraRefusal"


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
