"""The terminal commit of a hosted run carries the run trace nodes (ARBORA-HOST-R036)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.agent_host.port_services import RunTraceReceipts

RUN = "run:r36"
OWNER = SimpleNamespace(
    task_id=RUN,
    work_item_id="work:36",
    session=SimpleNamespace(tenant="t1"),
    claim={"lease_epoch": 2, "fencing_token": 7, "worker_id": "w1"},
)


def _receipts(answers: list[Mapping[str, Any]], sent: list[Mapping[str, Any]]):
    async def call(op: str, params: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
        sent.append(params)
        return answers.pop(0)

    return RunTraceReceipts(
        call,
        lambda ref: OWNER,
        lambda ref: {"outcome_bundle": {"b": 1}, "run_event": {"e": 1}},
        lambda value: repr(value).encode(),
    )


@pytest.mark.spec("ARBORA-HOST-R036")
def test_terminal_commit_carries_run_trace_nodes() -> None:
    sent: list[Mapping[str, Any]] = []
    receipts = _receipts([{"outcome": "answered"}], sent)
    steps = [{"name": "a", "outcome": "ok"}, {"name": "b", "outcome": "ok"}]
    asyncio.run(receipts.commit(RUN, steps))
    extension = sent[0]["outcome_extension"]
    nodes = extension["receipt_nodes"]
    assert extension["outcome_bundle"] == {"b": 1}
    assert [n["kind"] for n in nodes] == ["run_trace", "tool_call", "tool_call"]
    assert nodes[0]["node_id"] == f"receipt:{RUN}:0"
    assert {n["fence_token"] for n in nodes} == {7}
    assert sent[0]["outcome"] == "completed"


@pytest.mark.spec("ARBORA-HOST-R036")
def test_the_trace_node_holds_every_step_recursively() -> None:
    sent: list[Mapping[str, Any]] = []
    receipts = _receipts([{"outcome": "answered"}], sent)
    steps = [{"name": "only"}]
    asyncio.run(receipts.commit(RUN, steps))
    root = sent[0]["outcome_extension"]["receipt_nodes"][0]
    assert b"only" in root["properties_msgpack"]
    assert root["payload_ref"] == f"sha256:{root['payload_digest']}"


@pytest.mark.spec("ARBORA-HOST-R036")
def test_an_engine_refusal_passes_through_unchanged() -> None:
    receipts = _receipts(
        [{"outcome": "refused", "refusal_code": "RUN_TRACE_INVALID"}], []
    )
    with pytest.raises(RuntimeError, match="RUN_TRACE_INVALID"):
        asyncio.run(receipts.commit(RUN, [{"name": "x"}]))


@pytest.mark.spec("ARBORA-HOST-R036")
def test_a_run_with_no_steps_still_commits_the_trace_root() -> None:
    sent: list[Mapping[str, Any]] = []
    receipts = _receipts([{"outcome": "answered"}], sent)
    asyncio.run(receipts.commit(RUN, []))
    nodes = sent[0]["outcome_extension"]["receipt_nodes"]
    assert [n["kind"] for n in nodes] == ["run_trace"]
