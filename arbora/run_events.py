"""Publish and validate run events through EG's sole fenced durable authority."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from secrets import token_hex
from typing import Any

from arbora.admitted_run import AdmittedClaim, current_admitted_claim
from arbora.api.contracts.session import use_session
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

KINDS = frozenset({"text_delta", "tool_call", "tool_result", "state"})


class RunEventsUnavailable(RuntimeError):
    """The native event producer cannot supply a validated durable projection."""


def canonical_content(content: Mapping[str, Any]) -> tuple[str, str]:
    raw = json.dumps(
        dict(content), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    if len(raw.encode("utf-8")) > 1_048_576:
        raise ValueError("run event content exceeds native limit")
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_item(
    item: Any, *, work_item_id: str, run_id: str, owner_ref: str
) -> dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item, dict) else None
    if not (
        isinstance(item, dict)
        and item.get("work_item_id") == work_item_id
        and item.get("kind") == "orchestrator_task"
        and isinstance(metadata, dict)
        and metadata.get("a2a_schema") == "arbora-a2a-unary-v1"
        and metadata.get("a2a_owner_ref") == owner_ref
        and metadata.get("a2a_run_id") == run_id
    ):
        raise RunEventsUnavailable("durable run events are unavailable")
    return item


@dataclass
class RunEventPublisher:
    binding: AdmittedClaim
    client: Any
    index: int | None = None
    pending: dict[str, Any] | None = None
    nonce: str = field(default_factory=lambda: token_hex(16), repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def _identity(self) -> dict[str, Any]:
        self.binding.require_active()
        return {
            "tenant": self.binding.session.tenant,
            "work_item_id": self.binding.work_item_id,
            "run_id": self.binding.run_id,
        }

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        self.binding.require_active()
        try:
            with use_session(self.binding.session):
                result = await asyncio.wait_for(
                    getattr(self.client, method)(**payload), timeout=5.0
                )
        except PermissionError as exc:
            raise PermissionError("native run event authority refused") from exc
        except Exception as exc:
            raise RunEventsUnavailable("durable run events are unavailable") from exc
        self.binding.require_active()
        return result

    async def _initialize(self) -> None:
        page = await self._call(
            "get_events", {**self._identity(), "after_cursor": None, "limit": 1}
        )
        if (
            not isinstance(page, dict)
            or type(page.get("next_index")) is not int
            or page["next_index"] < 0
        ):
            raise RunEventsUnavailable("durable run events are unavailable")
        if self.binding.owner_ref is not None:
            validate_item(
                page.get("work_item"),
                work_item_id=self.binding.work_item_id,
                run_id=self.binding.run_id,
                owner_ref=self.binding.owner_ref,
            )
        self.index = page["next_index"]

    def _validate_receipt(self, result: Any, digest: str, retrying: bool) -> None:
        if not isinstance(result, dict):
            raise RunEventsUnavailable("native run event receipt is invalid")
        expected = {
            "work_item_id": self.binding.work_item_id,
            "run_id": self.binding.run_id,
            "payload_digest": digest,
        }
        if any(result.get(key) != value for key, value in expected.items()):
            raise RunEventsUnavailable("native run event receipt identity is invalid")
        if (
            type(result.get("event_index")) is not int
            or result["event_index"] != (self.index or 0) + 1
        ):
            raise RunEventsUnavailable("native run event receipt index is invalid")
        replayed = result.get("replayed")
        if type(replayed) is not bool or (replayed and not retrying):
            raise RunEventsUnavailable("native run event receipt replay is invalid")

    async def publish(self, kind: str, content: Mapping[str, Any]) -> None:
        if kind not in KINDS:
            raise ValueError("unsupported run event kind")
        raw, digest = canonical_content(content)
        async with self.lock:
            if self.index is None:
                await self._initialize()
            assert self.index is not None
            payload = {
                **self._identity(),
                "worker_id": self.binding.claim["lease_holder_ref"],
                "lease_epoch": self.binding.claim["lease_epoch"],
                "fencing_token": self.binding.claim["fencing_token"],
                "expected_index": self.index,
                "event": {"kind": kind, "content": raw, "payload_digest": digest},
                "idempotency_key": hashlib.sha256(
                    json.dumps([self.nonce, self.index, digest]).encode()
                ).hexdigest(),
            }
            if self.pending is not None and self.pending != payload:
                raise RunEventsUnavailable("run event write outcome is uncertain")
            retrying = self.pending is not None
            self.pending = payload
            result = await self._call("append_event", payload)
            self._validate_receipt(result, digest, retrying)
            self.index = result["event_index"]
            self.pending = None


@contextlib.asynccontextmanager
async def durable_run_event_scope(
    request: Any,
) -> AsyncIterator[RunEventPublisher | None]:
    binding = current_admitted_claim(request)
    if binding is None:
        yield None
        return
    yield await _publisher_for_binding(binding)


async def _publisher_for_binding(binding: AdmittedClaim) -> RunEventPublisher | None:
    existing: RunEventPublisher | None = binding.adapters.get("events")
    if existing is not None:
        return existing
    client = binding.engine.graph_compute.for_graph(
        CONTROL_GRAPH
    ).async_client.work_items
    if not await events_supported(client):
        return None
    publisher = RunEventPublisher(binding, client)
    binding.adapters["events"] = publisher
    return publisher


async def events_supported(client: Any) -> bool:
    """Probe connected native Health capabilities, not Python facade presence."""
    probe = getattr(client, "run_events_supported", None)
    if not callable(probe):
        return False
    try:
        return await asyncio.wait_for(probe(), timeout=5.0) is True
    except Exception:
        return False


def _event_content(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict) or event.get("kind") not in KINDS:
        raise RunEventsUnavailable("invalid durable event kind")
    raw = event.get("content")
    if not isinstance(raw, str) or len(raw.encode()) > 1_048_576:
        raise RunEventsUnavailable("invalid durable event payload")
    if hashlib.sha256(raw.encode()).hexdigest() != event.get("payload_digest"):
        raise RunEventsUnavailable("invalid durable event digest")
    try:
        content = json.loads(raw)
    except ValueError as exc:
        raise RunEventsUnavailable("invalid durable event payload") from exc
    if not isinstance(content, dict):
        raise RunEventsUnavailable("invalid durable event content")
    return content


def _event_cursor(cursor: Any) -> None:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
        raise RunEventsUnavailable("invalid durable event cursor")
    if any(ord(char) < 32 or ord(char) > 126 for char in cursor):
        raise RunEventsUnavailable("invalid durable event cursor")


def _validate_entry(entry: Any, previous: int | None, tail: int) -> int:
    if not isinstance(entry, dict):
        raise RunEventsUnavailable("invalid durable event")
    index = entry.get("event_index")
    if type(index) is not int or not 1 <= index <= tail:
        raise RunEventsUnavailable("invalid durable event index")
    if previous is not None and index != previous + 1:
        raise RunEventsUnavailable("non-contiguous durable events")
    _event_cursor(entry.get("cursor"))
    _event_content(entry.get("event"))
    return index


def validate_page(
    page: Any,
    *,
    work_item_id: str,
    run_id: str,
    owner_ref: str,
    after_cursor: str | None = None,
) -> dict[str, Any]:
    if not isinstance(page, dict):
        raise RunEventsUnavailable("durable run events are unavailable")
    validate_item(
        page.get("work_item"),
        work_item_id=work_item_id,
        run_id=run_id,
        owner_ref=owner_ref,
    )
    tail = page.get("next_index")
    if type(tail) is not int or tail < 0:
        raise RunEventsUnavailable("invalid durable event tail")
    events = page.get("events")
    if (
        not isinstance(events, list)
        or len(events) > 128
        or type(page.get("has_more")) is not bool
    ):
        raise RunEventsUnavailable("invalid durable event page")
    previous = None
    for entry in events:
        previous = _validate_entry(entry, previous, tail)
    _validate_page_end(page, events, after_cursor)
    committed_usage(page.get("committed_run_usage"), page["work_item"])
    return page


def _validate_page_end(
    page: dict[str, Any], events: list[Any], after_cursor: str | None
) -> None:
    if events and page.get("next_cursor") != events[-1]["cursor"]:
        raise RunEventsUnavailable("invalid durable event cursor")
    if page["has_more"] and not events:
        raise RunEventsUnavailable("invalid empty durable event page")
    _validate_page_coverage(page, events, after_cursor)


def _validate_page_coverage(
    page: dict[str, Any], events: list[Any], after_cursor: str | None
) -> None:
    if events and after_cursor is None and events[0]["event_index"] != 1:
        raise RunEventsUnavailable("durable event prefix is missing")
    if (
        events
        and not page["has_more"]
        and events[-1]["event_index"] != page["next_index"]
    ):
        raise RunEventsUnavailable("durable event tail is missing")


def committed_usage(receipt: Any, item: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the native owner-checked receipt without exposing its principal."""
    if receipt is None:
        return None
    metadata = item["metadata"]
    if not isinstance(receipt, dict) or (
        receipt.get("run_id") != metadata.get("a2a_run_id")
        or receipt.get("context_id") != metadata.get("a2a_context_id")
        or not isinstance(receipt.get("tenant"), str)
        or not isinstance(receipt.get("principal"), str)
        or not isinstance(receipt.get("counts"), dict)
    ):
        raise RunEventsUnavailable("invalid committed usage receipt")
    from arbora.api.contracts.agent_control_contracts import AgentExecutionResult

    try:
        validated = AgentExecutionResult(
            run_id=receipt["run_id"], output="", usage=receipt["counts"]
        )
    except ValueError as exc:
        raise RunEventsUnavailable("invalid committed usage counts") from exc
    return {
        "runId": receipt["run_id"],
        "taskId": item["work_item_id"],
        "contextId": receipt["context_id"],
        "scope": "turn",
        "counts": validated.usage,
    }


def a2a_event(
    task_id: str,
    item: dict[str, Any],
    entry: dict[str, Any],
    *,
    terminal_index: int | None = None,
    usage_receipt: Any = None,
) -> dict[str, Any]:
    """Project native facts into standard A2A events, retaining structured evidence."""
    event = entry["event"]
    content = json.loads(event["content"])
    metadata = {
        "arbora": {
            "runId": item["metadata"]["a2a_run_id"],
            "eventIndex": entry["event_index"],
            "event": {"kind": event["kind"], "content": content},
        }
    }
    base = {
        "taskId": task_id,
        "contextId": item["metadata"]["a2a_context_id"],
        "metadata": metadata,
    }
    if event["kind"] == "text_delta":
        text = content.get("delta")
        if not isinstance(text, str):
            raise RunEventsUnavailable("invalid durable text delta")
        part = content.get("part_index", 0)
        step = content.get("step", 0)
        return {
            **base,
            "kind": "artifact-update",
            "artifact": {
                "artifactId": f"text-{step}-{part}",
                "parts": [{"kind": "text", "text": text}],
            },
            "append": content.get("reset") is not True,
            "lastChunk": False,
        }
    state = content.get("state") if event["kind"] == "state" else ""
    if not isinstance(state, str):
        state = ""
    mapped = {
        "succeeded": "completed",
        "failed": "failed",
        "dead_letter": "failed",
        "cancelled": "canceled",
    }
    final = (
        state in mapped
        and state == item.get("status")
        and entry["event_index"] == terminal_index
    )
    if final and usage_receipt is not None:
        metadata["arbora"]["usage"] = committed_usage(usage_receipt, item)
    return {
        **base,
        "kind": "status-update",
        "status": {"state": mapped[state] if final else "working"},
        "final": final,
    }


@contextlib.asynccontextmanager
async def terminal_event_scope(
    binding: AdmittedClaim | None, *, enabled: bool, commit_options: dict[str, Any]
) -> AsyncIterator[None]:
    """Capture the native append tail while the admission carrier is still active."""
    if not enabled:
        yield
        return
    if binding is None:
        raise RunEventsUnavailable("native run admission is required")
    client = binding.engine.graph_compute.for_graph(
        CONTROL_GRAPH
    ).async_client.work_items
    publisher = RunEventPublisher(binding, client)
    binding.adapters["events"] = publisher
    await publisher._initialize()
    try:
        yield
    finally:
        if publisher.pending is not None:
            await publisher._initialize()
        commit_options["terminal_stream"] = {
            "run_id": binding.run_id,
            "expected_index": publisher.index,
        }
