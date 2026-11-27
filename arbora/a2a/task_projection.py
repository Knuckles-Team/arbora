"""Projecting a WorkItem to an A2A task: the held output, the persisted reply and the list cursor (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable as _Callable
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

from arbora.a2a.run_lifecycle import _MAX_OUTPUT_CHARS
from arbora.api.domain.a2a.models import (
    A2AArtifact,
    A2AReplyUnavailable,
    A2ATask,
    A2ATaskState,
    A2ATaskStatus,
)

logger = logging.getLogger(__name__)


_STATE_MAP: Mapping[str, A2ATaskState] = MappingProxyType(
    {
        "submitted": "submitted",
        "ready": "submitted",
        "leased": "working",
        "running": "working",
        "succeeded": "completed",
        "failed": "failed",
        "cancelled": "canceled",
        "dead_letter": "failed",
    }
)


class _ReplyUnavailable(RuntimeError):
    def __init__(self, reason: Any) -> None:
        super().__init__("durable A2A reply is unavailable")
        self.availability = A2AReplyUnavailable(reason=reason)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TaskProjection:
    """Projects a stored WorkItem and its reply to the A2A task shape.

    Built from the shared state and the calls it needs, with no reference to
    the authority that owns that state."""

    def __init__(
        self,
        *,
        engine: _Callable[..., Any],
        work_item_id: _Callable[..., Any],
        held_output_source: _Callable[[], Any],
        outputs: Any,
    ) -> None:
        self._engine = engine
        self._work_item_id = work_item_id
        self._get_held_output_source = held_output_source
        self.outputs = outputs

    def held_task(self, item: dict[str, Any], *, task_id: str) -> A2ATask:
        """The task of ``item``, with the held reply of a succeeded run."""
        return self._project(
            item, task_id=task_id, artifacts=self.outputs.artifacts(task_id)
        )

    def _project(
        self,
        item: dict[str, Any],
        *,
        task_id: str,
        artifacts: list[A2AArtifact] | None = None,
    ) -> A2ATask:
        raw_state = str(item.get("status") or "")
        state = self._state_of(item, raw_state)
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            raise RuntimeError("canonical WorkItem lacks A2A metadata")
        return A2ATask(
            id=task_id,
            context_id=str(metadata.get("a2a_context_id") or ""),
            status=A2ATaskStatus(
                state=state, timestamp=TaskProjection._item_timestamp(item)
            ),
            artifacts=artifacts if raw_state == "succeeded" else None,
            metadata={"arbora": self._public_metadata(item, metadata, task_id)},
        )

    @staticmethod
    def _state_of(item: dict[str, Any], raw_state: str) -> Any:
        """The A2A state of a canonical WorkItem status."""
        from .event_log import terminal_state

        state = (
            "failed"
            if terminal_state(item) == "failed" and raw_state == "cancelled"
            else _STATE_MAP.get(raw_state)
        )
        if state is None:
            raise RuntimeError("canonical WorkItem has an invalid A2A state")
        return state

    @staticmethod
    def _item_timestamp(item: dict[str, Any]) -> str | None:
        """The timestamp of the last update of a canonical WorkItem."""
        return TaskProjection._timestamp(
            item["updated_at_ms"] / 1000
            if isinstance(item.get("updated_at_ms"), (int, float))
            else item.get("updated_at")
        )

    def _public_metadata(
        self, item: dict[str, Any], metadata: dict[str, Any], task_id: str
    ) -> dict[str, Any]:
        """The public ``arbora`` block of a task: authority ref, run id and route."""
        from arbora.observability.trace_ontology import (
            trace_id as canonical_trace_id,
        )

        route = metadata.get("a2a_route")
        run_id = str(metadata.get("a2a_run_id") or task_id)
        return {
            "taskAuthorityRef": str(
                item.get("work_item_id") or self._work_item_id(task_id)
            ),
            "runId": run_id,
            "routing": route if isinstance(route, dict) else {},
            "runTraceRef": canonical_trace_id(run_id),
        }

    @staticmethod
    def _read_reply_payload(payload: Any, task_id: str) -> Any:
        """Decode the bounded durable payload before validating its identity."""
        from arbora.agent_runs import MAX_RUN_EVENT_PAYLOAD_BYTES

        if not isinstance(payload, str):
            raise RuntimeError("durable A2A reply is unavailable")
        try:
            if len(payload.encode("utf-8")) > MAX_RUN_EVENT_PAYLOAD_BYTES:
                raise ValueError("run event exceeds its payload bound")
            event = json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("durable A2A reply is unavailable") from exc
        TaskProjection._check_reply_marker(event, task_id)
        return event

    @staticmethod
    def _check_reply_marker(event: Any, task_id: str) -> None:
        """Refuse malformed markers before exposing a typed availability reason."""
        from arbora.agent_runs import RUN_EVENT_PAYLOAD_ERROR_SCHEMA

        if (
            isinstance(event, dict)
            and event.get("schema") == RUN_EVENT_PAYLOAD_ERROR_SCHEMA
        ):
            error = event.get("payload_error")
            if not (
                event.get("type") == "graph_complete"
                and event.get("run_id") == task_id
                and set(event)
                <= {"schema", "type", "run_id", "step", "status", "payload_error"}
                and type(event.get("step")) is int
                and 0 <= event["step"] <= 2**63 - 1
            ):
                raise RuntimeError("durable A2A reply is unavailable")
            TaskProjection._raise_reply_marker(event, error)

    @staticmethod
    def _raise_reply_marker(event: dict[str, Any], error: Any) -> None:
        """Validate marker details before raising their typed refusal."""
        from arbora.agent_runs import MAX_RUN_EVENT_PAYLOAD_BYTES

        if not (
            isinstance(error, dict)
            and set(error) == {"code", "max_bytes"}
            and type(error["max_bytes"]) is int
            and error["max_bytes"] == MAX_RUN_EVENT_PAYLOAD_BYTES
            and isinstance(error["code"], str)
            and event.get("status") in (None, "completed", "stopped")
            and error["code"] in {"payload_too_large", "payload_serialization_failed"}
        ):
            raise RuntimeError("durable A2A reply is unavailable")
        raise _ReplyUnavailable(error["code"])

    @staticmethod
    def _reply_text(event: Any, task_id: str) -> str:
        """Accept output only from the matching successful terminal run."""
        result = event.get("result") if isinstance(event, dict) else None
        if not (
            isinstance(event, dict)
            and event.get("type") == "graph_complete"
            and event.get("run_id") == task_id
            and isinstance(result, dict)
            and result.get("run_id") == task_id
            and result.get("status") in {"completed", "stopped"}
            and isinstance(result.get("output"), str)
        ):
            raise RuntimeError("durable A2A reply is unavailable")
        return cast(str, result["output"])

    def _persisted_artifacts(
        self, task_id: str, *, item: dict[str, Any]
    ) -> list[A2AArtifact] | None:
        """Read an existing terminal payload, never reconstruct a receipt."""
        return self._persisted_reply(task_id, item=item)[0]

    def _persisted_reply(
        self, task_id: str, *, item: dict[str, Any]
    ) -> tuple[list[A2AArtifact] | None, dict[str, Any] | None]:
        """The terminal reply and the held-output block its run emitted, if any."""
        if item.get("status") != "succeeded":
            return None, None
        run_id = str(item.get("metadata", {}).get("a2a_run_id") or task_id)
        engine, _work_engine = self._engine()
        rows = engine.query_cypher(
            "MATCH (e) WHERE e.type = 'RunEvent' "
            "AND e.run_id = $run_id AND e.task_id = $run_id "
            "AND e.event_type = $event_type "
            "RETURN e.payload AS payload LIMIT 2",
            {"run_id": run_id, "event_type": "graph_complete"},
        )
        if rows == []:
            return None, None
        if not isinstance(rows, list) or len(rows) != 1:
            raise RuntimeError("durable A2A reply is unavailable")
        row = rows[0]
        payload = row.get("payload") if isinstance(row, dict) else None
        event = self._read_reply_payload(payload, run_id)
        text = self._reply_text(event, run_id)
        if len(text) > _MAX_OUTPUT_CHARS:
            raise _ReplyUnavailable("reply_too_large")
        if not text:
            return None, None
        from .held_output import reply_artifact
        from .held_output_source import held_block

        held = held_block(
            event.get("result"), engine, task_id, self._get_held_output_source()
        )
        return [reply_artifact(task_id, text)], held

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
        rendered = str(value).strip()
        return rendered or None

    @staticmethod
    def _cursor(owner_ref: str, tenant: str, native_cursor: str) -> str:
        return f"{_digest({'tenant': tenant, 'owner': owner_ref, 'cursor': native_cursor})}.{native_cursor}"

    @classmethod
    def _decode_cursor(
        cls, cursor: str | None, *, owner_ref: str, tenant: str
    ) -> str | None:
        if cursor is None:
            return None
        digest, separator, native_cursor = cursor.partition(".")
        if (
            not separator
            or not native_cursor
            or digest
            != _digest({"tenant": tenant, "owner": owner_ref, "cursor": native_cursor})
        ):
            raise ValueError("A2A task cursor is invalid")
        return native_cursor

    @staticmethod
    def _validated_list_page(
        page: Any, limit: int, native_cursor: str | None
    ) -> tuple[list[Any], str | None]:
        """Validate bounded native rows and forward-only continuation together."""
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise RuntimeError("canonical WorkItem list returned invalid data")
        rows = page["items"]
        continuation = page.get("next_cursor")
        if len(rows) > limit or (
            continuation is not None
            and (
                not isinstance(continuation, str)
                or not continuation
                or continuation == native_cursor
            )
        ):
            raise RuntimeError("canonical WorkItem list returned invalid data")
        return rows, continuation
