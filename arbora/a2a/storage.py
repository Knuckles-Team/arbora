"""A2A task and conversation storage over the engine KV (ARBORA-A2A-R001.3).

:class:`EngineA2AStorage` implements the SDK storage protocol with nothing kept in
process: every task, context and conversation index is an engine KV entry under the
namespace ``a2a``, keyed ``a2a/<tenant>/<principal>/<context_id>/...``. A new host
process reads the same entries, so a ``contextId`` sent after a restart continues its
conversation. Each context is bound to the principal that opened it by one
compare-and-set owner entry; another principal naming it is refused exactly like an
absent context (JSON-RPC ``-32001``). Retention follows ``context_retention_days``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from fasta2a.schema import Artifact, Message, Task, TaskState, TaskStatus
from fasta2a.storage import Storage
from pydantic_core import to_jsonable_python

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "NAMESPACE",
    "A2AContextNotFound",
    "EngineA2AStorage",
    "engine_kv_send",
]

NAMESPACE = "a2a"
DEFAULT_RETENTION_DAYS = 30
DEFAULT_PAGE = 50
MAX_PAGE = 100
TITLE_CHARS = 80
_CAS_ATTEMPTS = 5
_DAY_MS = 86_400_000

#: ``send(method, params, idempotency_key)`` returns the engine payload.
EngineSend = Callable[[str, dict[str, Any], str | None], Awaitable[Any]]


class A2AContextNotFound(LookupError):
    """The context is absent or another principal's: JSON-RPC ``-32001``."""

    code = -32001


def _payload(result: Any) -> Any:
    return getattr(result, "payload", result)


def _encode(value: Any) -> bytes:
    return json.dumps(
        to_jsonable_python(value), sort_keys=True, separators=(",", ":")
    ).encode()


def _decode(raw: Any) -> Any:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            return json.loads(bytes(raw))
        except ValueError:
            return None
    return raw


def engine_kv_send(engine_provider: Callable[[], Any]) -> EngineSend:
    """Send KV methods as the verified session of the running request."""

    async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
        from arbora import work_items
        from arbora.api.ops.engine_calls import send_on_client
        from arbora.semantic_provisioning import CONTROL_GRAPH

        engine = engine_provider()
        if engine is None:
            raise RuntimeError("Arbora A2A storage is unavailable")
        routed = getattr(engine, "_session_client", None)
        client = routed() if callable(routed) else engine
        try:
            graph = work_items.native_owner_graph(
                getattr(engine, "_work_item_engine", engine)
            )
        except work_items.WorkItemBackendUnavailable:
            graph = CONTROL_GRAPH
        return await send_on_client(client, method, params, graph, key=key)

    return send


class EngineA2AStorage(Storage[Any]):
    """The SDK storage protocol over the engine KV; the engine is the only store."""

    def __init__(
        self,
        send: EngineSend,
        *,
        tenant: Callable[[], str],
        principal: Callable[[], str],
        clock: Callable[[], float] = time.time,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention must be at least one day")
        self._send = send
        self._tenant = tenant
        self._principal = principal
        self._clock = clock
        self._retention_ms = retention_days * _DAY_MS

    # -- keys ---------------------------------------------------------------------

    def _root(self) -> str:
        return f"a2a/{self._tenant()}/{self._principal()}"

    def _owner_key(self, context_id: str) -> str:
        return f"a2a/{self._tenant()}/_owner/{context_id}"

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    # -- engine KV ----------------------------------------------------------------

    async def _get(self, key: str) -> tuple[Any, int]:
        entry = _payload(
            await self._send("KvGet", {"namespace": NAMESPACE, "key": key}, None)
        )
        if isinstance(entry, Mapping) and "value" in entry:
            return _decode(entry["value"]), int(entry.get("version", 0))
        return None, 0

    async def _cas(self, key: str, value: Any, expected: int) -> bool:
        receipt = _payload(
            await self._send(
                "KvCas",
                {
                    "namespace": NAMESPACE,
                    "key": key,
                    "expected_version": expected,
                    "new": _encode(value),
                },
                None,
            )
        )
        return bool(isinstance(receipt, Mapping) and receipt.get("swapped"))

    async def _put(self, key: str, value: Any) -> None:
        for _ in range(_CAS_ATTEMPTS):
            _current, version = await self._get(key)
            if await self._cas(key, value, version):
                return
        raise RuntimeError("A2A storage write conflicted")

    async def _mutate(
        self, key: str, change: Callable[[Any], Any], *, missing: Exception
    ) -> Any:
        for _ in range(_CAS_ATTEMPTS):
            current, version = await self._get(key)
            if current is None:
                raise missing
            updated = change(current)
            if await self._cas(key, updated, version):
                return updated
        raise RuntimeError("A2A storage write conflicted")

    # -- ownership ----------------------------------------------------------------

    async def _claim(self, context_id: str, *, create: bool) -> None:
        """Bind a context to this principal, or refuse like an absent one."""
        owner, _version = await self._get(self._owner_key(context_id))
        if owner is None and create:
            await self._cas(self._owner_key(context_id), self._principal(), 0)
            owner, _version = await self._get(self._owner_key(context_id))
        if owner != self._principal():
            raise A2AContextNotFound(context_id)

    async def _touch(self, context_id: str, message: Message | None = None) -> None:
        key = f"{self._root()}/{context_id}/meta"
        current, _version = await self._get(key)
        title = (current or {}).get("title") or _title(message)
        await self._put(
            key,
            {
                "context_id": context_id,
                "updated_at": self._now_ms(),
                "expires_at": self._now_ms() + self._retention_ms,
                "title": title,
            },
        )

    # -- the SDK storage protocol -------------------------------------------------

    async def submit_task(self, context_id: str, message: Message) -> Task:
        await self._claim(context_id, create=True)
        task_id = str(uuid.uuid4())
        message["task_id"] = task_id
        message["context_id"] = context_id
        task = Task(
            id=task_id,
            context_id=context_id,
            kind="task",
            status=TaskStatus(
                state="submitted", timestamp=datetime.now(UTC).isoformat()
            ),
            history=[message],
        )
        await self._put(f"{self._root()}/{context_id}/task/{task_id}", task)
        await self._put(f"{self._root()}/_task/{task_id}", context_id)
        await self._touch(context_id, message)
        return task

    async def _task_key(self, task_id: str) -> str | None:
        context_id, _version = await self._get(f"{self._root()}/_task/{task_id}")
        if not isinstance(context_id, str):
            return None
        return f"{self._root()}/{context_id}/task/{task_id}"

    async def load_task(
        self, task_id: str, history_length: int | None = None
    ) -> Task | None:
        key = await self._task_key(task_id)
        if key is None:
            return None
        task, _version = await self._get(key)
        if task is None:
            return None
        if history_length and "history" in task:
            task["history"] = task["history"][-history_length:]
        return task

    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        new_artifacts: list[Artifact] | None = None,
        new_messages: list[Message] | None = None,
    ) -> Task:
        key = await self._task_key(task_id)
        if key is None:
            raise A2AContextNotFound(task_id)
        stamp = datetime.now(UTC).isoformat()

        def change(task: dict[str, Any]) -> dict[str, Any]:
            task["status"] = {"state": state, "timestamp": stamp}
            if new_artifacts:
                task.setdefault("artifacts", []).extend(
                    to_jsonable_python(new_artifacts)
                )
            for message in new_messages or ():
                message["task_id"] = task_id
                message["context_id"] = task["context_id"]
                task.setdefault("history", []).append(to_jsonable_python(message))
            return task

        task = await self._mutate(key, change, missing=A2AContextNotFound(task_id))
        await self._touch(task["context_id"])
        return task

    async def load_context(self, context_id: str) -> Any | None:
        try:
            await self._claim(context_id, create=False)
        except A2AContextNotFound:
            return None
        context, _version = await self._get(f"{self._root()}/{context_id}/context")
        return context

    async def update_context(self, context_id: str, context: Any) -> None:
        await self._claim(context_id, create=True)
        await self._put(f"{self._root()}/{context_id}/context", context)
        await self._touch(context_id)

    # -- the caller's conversations -----------------------------------------------

    async def list_contexts(
        self, *, limit: int = DEFAULT_PAGE, cursor: str | None = None
    ) -> dict[str, Any]:
        """The verified caller's conversations, newest first, one bounded page."""
        size = max(1, min(int(limit), MAX_PAGE))
        now = self._now_ms()
        metas = [m for m in await self._metas() if m["expires_at"] > now]
        metas.sort(key=lambda m: (-m["updated_at"], m["context_id"]))
        if cursor:
            at, _sep, last = cursor.partition(":")
            metas = [
                m
                for m in metas
                if (-m["updated_at"], m["context_id"]) > (-int(at), last)
            ]
        page = metas[:size]
        more = len(metas) > size
        return {
            "contexts": [
                {
                    "contextId": m["context_id"],
                    "updated_at": m["updated_at"],
                    "title": m["title"],
                }
                for m in page
            ],
            "next_cursor": f"{page[-1]['updated_at']}:{page[-1]['context_id']}"
            if more
            else None,
        }

    async def _metas(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            rows, cursor = await self._scan(cursor)
            found.extend(_metas_of(rows))
            if not cursor:
                return found

    async def _scan(self, cursor: str | None) -> tuple[list[Any], str | None]:
        request: dict[str, Any] = {
            "namespace": NAMESPACE,
            "prefix": f"{self._root()}/",
            "limit": 200,
        }
        if cursor:
            request["cursor"] = cursor
        page = _payload(await self._send("KvScan", request, None))
        if not isinstance(page, Mapping):
            return [], None
        return list(page.get("entries", [])), page.get("next_cursor")


def _metas_of(rows: list[Any]) -> list[dict[str, Any]]:
    """The conversation meta entries among scanned KV rows."""
    metas = (
        _decode(row.get("value"))
        for row in rows
        if str(row.get("key", "")).endswith("/meta")
    )
    return [meta for meta in metas if isinstance(meta, dict) and "context_id" in meta]


def _title(message: Message | None) -> str:
    """The first user text of the conversation, cut to 80 characters."""
    for part in (message or {}).get("parts", ()):
        if part.get("kind") == "text" and part.get("text"):
            return str(part["text"])[:TITLE_CHARS]
    return ""
