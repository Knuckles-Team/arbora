"""RunEvent payloads remain complete JSON within one shared serialized bound."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from arbora import agent_runs
from arbora.a2a.authority import (
    WorkItemA2AAuthority,
    remember_task_output,
    task_output_artifacts,
)

pytestmark = pytest.mark.spec("ARBORA-HOST-R001.2.1", "ARBORA-A2A-R001")
TASK = "a2a-" + "c" * 64


def terminal(output: str = "reply", status: str = "completed") -> dict[str, Any]:
    return {
        "type": "graph_complete",
        "run_id": TASK,
        "step": 1,
        "status": status,
        "result": {
            "run_id": TASK,
            "output": output,
            "status": status,
            "steps": 1,
            "usage": {"requests": 1},
            "error": None,
            "execution_mode": "graph",
        },
    }


def sink(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    engine = SimpleNamespace(
        create_node=lambda node_id, label, properties: records.append(properties)
    )
    monkeypatch.setattr(agent_runs, "_engine", lambda: engine)
    return records


def read(records: list[dict[str, Any]], status: str = "succeeded") -> Any:
    authority = WorkItemA2AAuthority(
        lambda: SimpleNamespace(
            query_cypher=lambda *args: [{"payload": row["payload"]} for row in records]
        )
    )
    return authority._persisted_artifacts(TASK, item={"status": status})


@pytest.mark.parametrize("character", ["a", "漢", "\x00", "😀", '"'])
@pytest.mark.parametrize("status", ["completed", "stopped"])
def test_maximum_reply_roundtrips_without_mutating_event(
    monkeypatch: pytest.MonkeyPatch, character: str, status: str
) -> None:
    records = sink(monkeypatch)
    output = character * 65_536
    event = terminal(output, status)
    original = deepcopy(event)
    agent_runs.record_run_event(event)
    assert event == original
    assert len(records) == 1
    payload = records[0]["payload"]
    assert len(payload.encode("utf-8")) <= agent_runs.MAX_RUN_EVENT_PAYLOAD_BYTES
    assert json.loads(payload) == original
    artifacts = read(records)
    assert artifacts is not None
    assert artifacts[0].parts[0].text == output
    remember_task_output(TASK, output)
    cached_artifacts = task_output_artifacts(TASK)
    assert cached_artifacts is not None
    assert cached_artifacts[0].parts[0].text == output


def at_limit(extra: int = 0) -> dict[str, Any]:
    event = {**terminal(), "other_event_field": ""}
    base = len(json.dumps(event, sort_keys=True, default=str))
    event["other_event_field"] = "x" * (
        agent_runs.MAX_RUN_EVENT_PAYLOAD_BYTES - base + extra
    )
    return event


def test_exact_serialized_limit_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    records = sink(monkeypatch)
    event = at_limit()
    agent_runs.record_run_event(event)
    assert (
        len(records[0]["payload"].encode("utf-8"))
        == agent_runs.MAX_RUN_EVENT_PAYLOAD_BYTES
    )
    assert json.loads(records[0]["payload"]) == event
    assert read(records)[0].parts[0].text == "reply"


def test_one_byte_over_limit_keeps_completion_and_marks_body_unavailable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    records = sink(monkeypatch)
    event = at_limit(1)
    event["other_event_field"] = "private" + event["other_event_field"][7:]
    agent_runs.record_run_event(event)
    assert len(records) == 1
    marker = json.loads(records[0]["payload"])
    assert marker == {
        "schema": agent_runs.RUN_EVENT_PAYLOAD_ERROR_SCHEMA,
        "type": "graph_complete",
        "run_id": TASK,
        "step": 1,
        "status": "completed",
        "payload_error": {
            "code": "payload_too_large",
            "max_bytes": agent_runs.MAX_RUN_EVENT_PAYLOAD_BYTES,
        },
    }
    assert "payload_too_large" in caplog.text
    with pytest.raises(RuntimeError, match="reply is unavailable"):
        read(records)
    assert "private" not in caplog.text
    with pytest.raises(RuntimeError, match="reply is unavailable"):
        read([{"payload": json.dumps(event, sort_keys=True, default=str)}])


def test_serialization_failure_keeps_identity_without_private_diagnostics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Unserializable:
        def __str__(self) -> str:
            raise RuntimeError("private error value")

    records = sink(monkeypatch)
    agent_runs.record_run_event({**terminal(), "extra": Unserializable()})
    agent_runs.record_run_event({**terminal(), "extra": float("nan")})
    assert len(records) == 2
    for record in records:
        marker = json.loads(record["payload"])
        assert marker["run_id"] == TASK and marker["type"] == "graph_complete"
        assert marker["payload_error"]["code"] == "payload_serialization_failed"
        assert "result" not in marker
    assert "payload_serialization_failed" in caplog.text
    assert "private error value" not in caplog.text


def test_larger_output_is_preserved_as_event_but_never_claimed_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = sink(monkeypatch)
    output = "a" * 65_537
    event = terminal(output)
    agent_runs.record_run_event(event)
    assert json.loads(records[0]["payload"]) == event
    remember_task_output(TASK, "old reply")
    remember_task_output(TASK, output)
    assert task_output_artifacts(TASK) is None
    with pytest.raises(RuntimeError, match="reply is unavailable"):
        read(records)


def test_failed_run_never_exposes_partial_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = sink(monkeypatch)
    event = {**terminal("partial", "failed"), "type": "error"}
    agent_runs.record_run_event(event)
    assert json.loads(records[0]["payload"]) == event
    assert read(records, status="failed") is None


def test_legacy_truncated_json_is_not_repaired() -> None:
    with pytest.raises(RuntimeError, match="reply is unavailable"):
        read([{"payload": json.dumps(terminal("a" * 65_536))[:65_536]}])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["payload_too_large", "payload_serialization_failed", "reply_too_large"]
)
async def test_task_read_preserves_completion_with_typed_unavailable_reply(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    records = sink(monkeypatch)
    if failure == "payload_too_large":
        event = at_limit(1)
    elif failure == "payload_serialization_failed":
        event = {**terminal(), "extra": float("nan")}
    else:
        event = terminal("a" * 65_537)
    agent_runs.record_run_event(event)
    assert len(records) == 1
    authority = WorkItemA2AAuthority(
        lambda: SimpleNamespace(
            query_cypher=lambda *args: [{"payload": records[0]["payload"]}]
        )
    )
    session = SimpleNamespace(tenant="tenant", graph="graph")
    monkeypatch.setattr(authority, "_session", lambda scope: session)
    monkeypatch.setattr(
        authority,
        "_authorized_item",
        lambda *args: {
            "status": "succeeded",
            "metadata": {"a2a_context_id": "a2a-context-" + "d" * 64},
        },
    )
    task = await authority.get(TASK)
    assert task is not None and task.status.state == "completed"
    assert task.artifacts is None
    assert task.metadata["arbora"]["reply"] == {
        "status": "unavailable",
        "reason": failure,
    }


@pytest.mark.parametrize("change", ["run_id", "code", "step", "status"])
def test_malformed_payload_error_marker_refuses(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    records = sink(monkeypatch)
    agent_runs.record_run_event(at_limit(1))
    marker = json.loads(records[0]["payload"])
    if change == "run_id":
        marker["run_id"] = "foreign"
    elif change == "code":
        marker["payload_error"]["code"] = []
    elif change == "step":
        marker["step"] = True
    else:
        marker["status"] = "failed"
    with pytest.raises(RuntimeError, match="reply is unavailable"):
        read([{"payload": json.dumps(marker)}])
