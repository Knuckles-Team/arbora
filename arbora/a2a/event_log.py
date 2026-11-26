"""The durable A2A event cursor and run-event publisher (ARBORA-A2A-R001.2).

Every A2A-visible run event (a status update or an artifact update) is written
to the engine with its ``event_seq`` before it is delivered. The record lives in
the engine's versioned KV store. The per-task ``event_seq`` lives in the
WorkItem metadata and advances by one compare-and-set under the lease fence, so
two writers cannot both claim a sequence and a restart loses nothing. Nothing
here holds correctness in process memory: the in-process signals only wake a
waiting stream, which then re-reads the durable record.

Every payload passes the one output redactor before it is written
(``arbora.api.invoke.screening``).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from arbora.api.invoke.screening import redact_result

__all__ = [
    "A2AEventFenceLost",
    "A2AEventGap",
    "A2AEventLog",
    "A2AEventStore",
    "A2AEventStream",
    "A2AEventStreams",
    "EngineKvEventStore",
    "StreamClock",
    "SystemStreamClock",
    "wire_event",
]

#: The WorkItem metadata key that holds the highest committed ``event_seq``.
EVENT_SEQ_KEY = "a2a_event_seq"
#: The engine KV namespace that holds the event records.
EVENT_NAMESPACE = "a2a_events"
#: A lease-less writer (a cancellation) outranks any run's fencing token.
_EXTERNAL_FENCE = 2**62
_READ_BATCH = 64
_POLL_SECONDS = 1.0
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})


class A2AEventGap(RuntimeError):
    """The requested cursor is ahead of the log or a record is missing."""


class A2AEventFenceLost(RuntimeError):
    """Another writer owns the sequence; this writer did not publish."""


class StreamClock(Protocol):
    """The injected time and wake-up source; tests never sleep."""

    def now_ms(self) -> int: ...

    async def wait(self, signal: asyncio.Event, timeout_s: float) -> None:
        """Return when ``signal`` is set or ``timeout_s`` has elapsed."""


class SystemStreamClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)

    async def wait(self, signal: asyncio.Event, timeout_s: float) -> None:
        try:
            await asyncio.wait_for(signal.wait(), timeout_s)
        except TimeoutError:
            return


class A2AEventStore(Protocol):
    """Versioned records keyed by task and sequence (the engine KV shape)."""

    async def get(self, key: str) -> tuple[dict[str, Any], int] | None: ...

    async def cas(
        self, key: str, expected_version: int, record: dict[str, Any]
    ) -> bool: ...


class EngineKvEventStore:
    """``A2AEventStore`` over the engine ``KvGet`` and ``KvCas`` methods."""

    def __init__(self, engine_provider: Callable[[], Any]) -> None:
        self._engine_provider = engine_provider

    async def _send(
        self, method: str, params: dict[str, Any], key: str | None = None
    ) -> Any:
        engine = self._engine_provider()
        if engine is None:
            raise RuntimeError("Arbora event authority is unavailable")
        routed = getattr(engine, "_session_client", None)
        client = routed() if callable(routed) else engine
        result = await client._send(method, params, None, idempotency_key=key)
        return getattr(result, "payload", result)

    async def get(self, key: str) -> tuple[dict[str, Any], int] | None:
        entry = await self._send(
            "KvGet", {"namespace": EVENT_NAMESPACE, "key": key, "shared": False}
        )
        if not isinstance(entry, Mapping) or "value" not in entry:
            return None
        value = json.loads(bytes(entry["value"]))
        return value, int(entry.get("version", 0))

    async def cas(
        self, key: str, expected_version: int, record: dict[str, Any]
    ) -> bool:
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        receipt = await self._send(
            "KvCas",
            {
                "namespace": EVENT_NAMESPACE,
                "key": key,
                "shared": False,
                "expected_version": expected_version,
                "new": raw,
            },
            f"a2a-event:{key}:{expected_version}:{len(raw)}",
        )
        return bool(isinstance(receipt, Mapping) and receipt.get("swapped"))


def _key(item_id: str, seq: int) -> str:
    return f"{item_id}/{seq:012d}"


def wire_event(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project one stored record as the A2A event it carries."""
    metadata = {"event_seq": record["event_seq"]}
    base = {
        "taskId": record["task_id"],
        "contextId": record["context_id"],
        "metadata": metadata,
    }
    if record["kind"] == "artifact-update":
        return {
            **base,
            "kind": "artifact-update",
            "artifact": record["artifact"],
            "append": False,
            "lastChunk": True,
        }
    stamp = datetime.fromtimestamp(record["timestamp_ms"] / 1000, tz=UTC).isoformat()
    return {
        **base,
        "kind": "status-update",
        "status": {"state": record["state"], "timestamp": stamp},
        "final": bool(record["final"]),
    }


class A2AEventLog:
    """Durable per-task event sequence, publisher and reader."""

    def __init__(
        self,
        engine_provider: Callable[[], Any],
        *,
        store: A2AEventStore | None = None,
        clock: StreamClock | None = None,
    ) -> None:
        self._engine_provider = engine_provider
        self.store: A2AEventStore = store or EngineKvEventStore(engine_provider)
        self.clock: StreamClock = clock or SystemStreamClock()
        # Wake-up signals of waiting streams. Never read for correctness.
        self._waiters: dict[str, set[asyncio.Event]] = {}

    def _work_engine(self) -> Any:
        engine = self._engine_provider()
        if engine is None:
            raise RuntimeError("Arbora task authority is unavailable")
        return getattr(engine, "_work_item_engine", engine)

    async def _item(self, item_id: str) -> dict[str, Any]:
        from arbora import work_items

        item = await asyncio.to_thread(
            work_items.get_work_item, self._work_engine(), item_id
        )
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            raise A2AEventFenceLost("the WorkItem is unavailable")
        return item

    async def publish(
        self,
        item_id: str,
        draft: Mapping[str, Any],
        *,
        claim: Mapping[str, Any] | None,
    ) -> int:
        """Write one event and advance ``event_seq`` under the lease fence.

        ``claim`` is the live lease of the run, or ``None`` for a lease-less
        writer such as a cancellation. Raises ``A2AEventFenceLost`` when another
        writer holds the sequence.
        """
        from arbora import work_items

        item = await self._item(item_id)
        metadata = dict(item["metadata"])
        sequence = int(metadata.get(EVENT_SEQ_KEY, 0)) + 1
        fence = int(claim["fencing_token"]) if claim is not None else _EXTERNAL_FENCE
        record = {
            **redact_result(dict(draft))[0],
            "event_seq": sequence,
            "timestamp_ms": self.clock.now_ms(),
            "fence": fence,
        }
        await self._write(item_id, sequence, record)
        applied = await asyncio.to_thread(
            work_items.cas_work_item_metadata,
            self._work_engine(),
            item,
            metadata={**metadata, EVENT_SEQ_KEY: sequence},
            claim=claim,
        )
        if not applied:
            raise A2AEventFenceLost("the event sequence advanced elsewhere")
        for waiter in tuple(self._waiters.get(item_id, ())):
            waiter.set()
        return sequence

    async def _write(self, item_id: str, sequence: int, record: dict[str, Any]) -> None:
        """Write the record unless a writer with an equal or higher fence did."""
        key = _key(item_id, sequence)
        existing = await self.store.get(key)
        version = 0
        if existing is not None:
            stored, version = existing
            if int(stored.get("fence", 0)) >= record["fence"]:
                raise A2AEventFenceLost("the event sequence advanced elsewhere")
        if not await self.store.cas(key, version, record):
            raise A2AEventFenceLost("the event sequence advanced elsewhere")

    async def read(
        self, item: Mapping[str, Any], after_seq: int, limit: int = _READ_BATCH
    ) -> list[dict[str, Any]]:
        """The committed events after ``after_seq``, in order, at most ``limit``.

        Raises ``A2AEventGap`` for a cursor beyond the committed sequence or a
        committed record the store does not hold.
        """
        committed = int(dict(item["metadata"]).get(EVENT_SEQ_KEY, 0))
        if after_seq > committed:
            raise A2AEventGap("the cursor is ahead of the committed events")
        records: list[dict[str, Any]] = []
        for sequence in range(after_seq + 1, min(committed, after_seq + limit) + 1):
            stored = await self.store.get(_key(str(item["work_item_id"]), sequence))
            if stored is None or stored[0].get("event_seq") != sequence:
                raise A2AEventGap("a committed event is missing from the log")
            records.append(stored[0])
        return records

    def subscribe(self, item_id: str) -> asyncio.Event:
        signal = asyncio.Event()
        self._waiters.setdefault(item_id, set()).add(signal)
        return signal

    def unsubscribe(self, item_id: str, signal: asyncio.Event) -> None:
        waiters = self._waiters.get(item_id)
        if waiters is not None:
            waiters.discard(signal)
            if not waiters:
                del self._waiters[item_id]


class A2AEventStream:
    """One reader's position in one task's events.

    ``batch`` returns the next committed events; ``wait`` blocks until a writer
    signals or the poll interval elapses. The stream is finished once it has
    returned the terminal event.
    """

    def __init__(
        self,
        log: A2AEventLog,
        lookup: Callable[[], Any],
        item_id: str,
        after_seq: int,
    ) -> None:
        self._log = log
        self._lookup = lookup
        self._item_id = item_id
        self._seq = after_seq
        self._signal = log.subscribe(item_id)
        self.finished = False

    async def batch(self) -> list[dict[str, Any]]:
        self._signal.clear()
        item = await self._lookup()
        if item is None:
            raise A2AEventGap("the task is no longer available")
        records = await self._log.read(item, self._seq)
        if records:
            self._seq = int(records[-1]["event_seq"])
            self.finished = any(record["final"] for record in records)
            return records
        if item.get("status") in _TERMINAL and self._seq == int(
            item["metadata"].get(EVENT_SEQ_KEY, 0)
        ):
            await self._finish_terminal(item)
        return []

    async def _finish_terminal(self, item: Mapping[str, Any]) -> None:
        """A terminal task with every event read must have ended on a final one."""
        last = await self._log.store.get(_key(self._item_id, self._seq))
        if self._seq < 1 or last is None or not last[0].get("final"):
            raise A2AEventGap("the task is terminal without a terminal event")
        self.finished = True

    async def wait(self) -> None:
        await self._log.clock.wait(self._signal, _POLL_SECONDS)

    def close(self) -> None:
        self._log.unsubscribe(self._item_id, self._signal)


class A2AEventStreams:
    """The streaming authority the Agent Card derives ``streaming`` from.

    ``resume_from_cursor`` opens a stream after the last ``event_seq`` the caller
    saw. A task that is absent or belongs to another caller answers ``None``.
    """

    def __init__(self, log: A2AEventLog, authority: Any) -> None:
        self.log = log
        self._authority = authority

    async def resume_from_cursor(
        self, task_id: str, after_seq: int = 0
    ) -> A2AEventStream | None:
        item = await self._authority.owned_item(task_id)
        if item is None:
            return None
        await self.log.read(item, after_seq, 0)  # refuse a cursor ahead of the log
        item_id = str(item["work_item_id"])
        return A2AEventStream(
            self.log, lambda: self._authority.owned_item(task_id), item_id, after_seq
        )
