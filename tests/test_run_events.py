"""Native event adapter contracts; fixtures are not live stack evidence."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.admitted_run import AdmittedClaim
from arbora.run_events import (
    RunEventPublisher,
    RunEventsUnavailable,
    canonical_content,
    events_supported,
    validate_page,
)


class Session:
    tenant = "tenant"

    def engine_verified_context(self) -> object:
        return object()


def item() -> dict[str, Any]:
    return {
        "work_item_id": "work-item:test",
        "kind": "orchestrator_task",
        "metadata": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": "owner",
            "a2a_run_id": "run",
        },
    }


def page() -> dict[str, Any]:
    raw, digest = canonical_content({"text": "hello"})
    return {
        "work_item": item(),
        "events": [
            {
                "event_index": 1,
                "cursor": "native-cursor",
                "event": {
                    "kind": "text_delta",
                    "content": raw,
                    "payload_digest": digest,
                },
            }
        ],
        "next_cursor": "native-cursor",
        "next_index": 1,
        "has_more": False,
    }


def validate(value: Any) -> Any:
    return validate_page(
        value, work_item_id="work-item:test", run_id="run", owner_ref="owner"
    )


@pytest.mark.spec("ARBORA-A2A-R010")
def test_reader_checks_scope_and_integrity() -> None:
    assert validate(page())["next_index"] == 1
    foreign = page()
    foreign["work_item"]["metadata"]["a2a_owner_ref"] = "foreign"
    with pytest.raises(RunEventsUnavailable):
        validate(foreign)
    corrupted = page()
    corrupted["events"][0]["event"]["content"] = '{"text":"changed"}'
    with pytest.raises(RunEventsUnavailable):
        validate(corrupted)


@pytest.mark.asyncio
async def test_capability_requires_connected_native_probe() -> None:
    assert not await events_supported(SimpleNamespace(append_event=lambda: None))


@pytest.mark.asyncio
async def test_uncertain_append_retries_exact_payload_without_duplicate(
    monkeypatch: Any,
) -> None:
    import contextlib

    import arbora.run_events as module

    monkeypatch.setattr(module, "use_session", lambda _: contextlib.nullcontext())
    calls = []

    class Client:
        async def get_events(self, **kwargs: Any) -> Any:
            return {"work_item": item(), "next_index": 0}

        async def append_event(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError("unknown write outcome")
            return {
                "work_item_id": "work-item:test",
                "run_id": "run",
                "event_index": 1,
                "payload_digest": kwargs["event"]["payload_digest"],
                "replayed": True,
            }

    binding = AdmittedClaim(
        None,
        Session(),
        "run",
        "work-item:test",
        {
            "lease_holder_ref": "worker",
            "lease_epoch": 1,
            "fencing_token": 1,
            "lease_expires_at_ms": time.time_ns() // 1_000_000 + 60000,
        },
        owner_ref="owner",
    )
    publisher = RunEventPublisher(binding, Client())
    with pytest.raises(RunEventsUnavailable):
        await publisher.publish("text_delta", {"text": "hello"})
    with pytest.raises(RunEventsUnavailable, match="uncertain"):
        await publisher.publish("text_delta", {"text": "different"})
    await publisher.publish("text_delta", {"text": "hello"})
    assert calls[0] == calls[1]
    assert publisher.index == 1
    binding.active = False
    with pytest.raises(RuntimeError, match="closed"):
        await publisher.publish("state", {"state": "completed"})


def test_model_state_cannot_claim_terminal_before_committed_tail() -> None:
    from arbora.run_events import a2a_event

    native = item()
    native["metadata"]["a2a_context_id"] = "context"
    native["status"] = "succeeded"
    entry = {
        "event_index": 1,
        "event": {"kind": "state", "content": '{"state":"succeeded"}'},
    }
    assert a2a_event("task", native, entry, terminal_index=2)["final"] is False
    assert a2a_event("task", native, entry, terminal_index=1)["final"] is True
    native["status"] = "running"
    assert a2a_event("task", native, entry, terminal_index=1)["final"] is False


@pytest.mark.spec("ARBORA-A2A-R001.4")
def test_actual_au_delta_contract_projects_standard_artifact() -> None:
    from arbora.run_events import a2a_event

    native = item()
    native["metadata"]["a2a_context_id"] = "context"
    entry = {
        "event_index": 1,
        "event": {
            "kind": "text_delta",
            "content": '{"delta":"hello","step":2,"part_index":0,"reset":true}',
        },
    }
    projected = a2a_event("task", native, entry)
    assert projected["kind"] == "artifact-update"
    assert projected["artifact"]["parts"] == [{"kind": "text", "text": "hello"}]
    assert projected["artifact"]["artifactId"] == "text-2-0"
    assert projected["append"] is False


@pytest.mark.spec("ARBORA-A2A-R001.3")
@pytest.mark.asyncio
async def test_existing_admission_never_starts_another_stream_execution(
    monkeypatch: Any,
) -> None:
    import asyncio

    from arbora import work_items
    from arbora.a2a.authority import WorkItemA2AAuthority

    authority = WorkItemA2AAuthority(lambda: None)
    gate = asyncio.Event()
    executions = []

    async def run(*args: Any, **kwargs: Any) -> None:
        executions.append(kwargs["item_id"])
        await gate.wait()

    async def read_events(*args: Any) -> Any:
        return {"events": []}

    monkeypatch.setattr(authority, "read_events", read_events)
    monkeypatch.setattr(authority, "_run", run)
    monkeypatch.setattr(authority, "_project", lambda value, **kwargs: value)
    monkeypatch.setattr(
        work_items, "get_work_item", lambda *args: {"status": "running"}
    )
    args = {"item_id": "work-item:test", "task_id": "work-item:test"}
    await authority._start_or_replay(
        None, {"status": "ready"}, created=True, streaming=True, arguments=args
    )
    await authority._start_or_replay(
        None, {"status": "running"}, created=False, streaming=True, arguments=args
    )
    await asyncio.sleep(0)
    assert executions == ["work-item:test"]
    gate.set()
    await asyncio.gather(*authority._running)


@pytest.mark.asyncio
async def test_native_reader_preflight_failure_starts_no_execution(
    monkeypatch: Any,
) -> None:
    from arbora.a2a.authority import WorkItemA2AAuthority

    authority = WorkItemA2AAuthority(lambda: None)

    async def unavailable(*args: Any) -> Any:
        raise RunEventsUnavailable("native reader unavailable")

    monkeypatch.setattr(authority, "read_events", unavailable)
    with pytest.raises(RunEventsUnavailable):
        await authority._start_stream(None, {"task_id": "work-item:test"})
    assert authority._running == set()


@pytest.mark.parametrize("missing", ["prefix", "tail"])
def test_reader_refuses_missing_committed_event_coverage(missing: str) -> None:
    value = page()
    value["next_index"] = 2
    if missing == "prefix":
        value["events"][0]["event_index"] = 2
    with pytest.raises(RunEventsUnavailable, match="missing"):
        validate(value)


@pytest.mark.asyncio
async def test_admitted_publisher_cannot_silently_downgrade_after_preflight() -> None:
    from arbora.run_events import _publisher_for_binding

    binding = AdmittedClaim(None, Session(), "run", "work-item:test", {})
    publisher = RunEventPublisher(binding, None)
    binding.adapters["events"] = publisher
    assert await _publisher_for_binding(binding) is publisher
