"""Canonical WorkItem/RunTrace authority used by every unary A2A surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from agent_connector_sdk.privacy import persistence_reference

from .models import (
    A2AArtifact,
    A2AMessage,
    A2AReplyUnavailable,
    A2ARouteDecision,
    A2ATask,
    A2ATaskState,
    A2ATaskStatus,
    A2ATextPart,
)
from .routing import A2AAssemblyUnavailable

__all__ = [
    "A2AContextUnavailable",
    "A2AIdempotencyConflict",
    "A2AStreamingUnavailable",
    "A2ATransitionHistoryUnavailable",
    "A2ATaskAuthority",
    "A2ATaskNotCancelable",
    "WorkItemA2AAuthority",
]

_TASK_PREFIX = "a2a-"
_WORK_ITEM_PREFIX = "workitem:orchestrator:"
_A2A_METADATA_SCHEMA = "arbora-a2a-unary-v1"
#: How long one agent run may hold its WorkItem lease.
_RUN_LEASE_SECONDS = 600.0
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})
#: Agent reply text of recent runs, by task id. A WorkItem holds only the
#: result reference; the durable record is the run's RunEvents in EG. This
#: bounded process-local view serves immediate dispatch/list projections;
#: tasks/get reads the durable record rather than trusting this cache.
_OUTPUTS: OrderedDict[str, str] = OrderedDict()
_MAX_OUTPUTS = 1024
_MAX_OUTPUT_CHARS = 65_536


def remember_task_output(task_id: str, output: Any) -> None:
    """Keep the reply text of one finished run for its task reads."""
    if not task_id or output in (None, ""):
        return
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    if len(text) > _MAX_OUTPUT_CHARS:
        _OUTPUTS.pop(task_id, None)
        return
    _OUTPUTS[task_id] = text
    _OUTPUTS.move_to_end(task_id)
    while len(_OUTPUTS) > _MAX_OUTPUTS:
        _OUTPUTS.popitem(last=False)


def task_output_artifacts(task_id: str) -> list[A2AArtifact] | None:
    """The agent's reply to ``task_id`` as the task's one artifact, when known."""
    text = _OUTPUTS.get(task_id)
    if not text:
        return None
    return [
        A2AArtifact(
            artifact_id=f"{task_id}:reply",
            name="reply",
            parts=[A2ATextPart(text=text)],
        )
    ]


_STATE_MAP: dict[str, A2ATaskState] = {
    "submitted": "submitted",
    "ready": "submitted",
    "leased": "working",
    "running": "working",
    "succeeded": "completed",
    "failed": "failed",
    "cancelled": "canceled",
    "dead_letter": "failed",
}


class _ReplyUnavailable(RuntimeError):
    def __init__(self, reason: Any) -> None:
        super().__init__("durable A2A reply is unavailable")
        self.availability = A2AReplyUnavailable(reason=reason)


class A2AContextUnavailable(RuntimeError):
    """The durable context ownership reader could not supply a bounded answer."""


class A2AIdempotencyConflict(RuntimeError):
    """An idempotency key was replayed with a different logical request."""


class A2ATaskNotCancelable(RuntimeError):
    """A task is unknown, terminal, or held by a non-cancelable authority."""


class A2AStreamingUnavailable(RuntimeError):
    """Durable task streaming/resubscribe has no event-cursor backing yet.

    ARBORA-A2A-R001 adds durable task streaming and resubscribe without a
    second task store. That bounded, restart-safe event-cursor backing does
    not exist yet, so ``tasks/resubscribe`` must fail closed rather than
    silently succeed or be reported as an unrecognized method.
    """


class A2ATransitionHistoryUnavailable(RuntimeError):
    """No reachable durable transition-history authority backs ``tasks/get`` history.

    ARBORA-A2A-R012.2: when no ``A2ATransitionHistoryAuthority`` is wired or
    the wired one is unreachable, history requests fail closed with this
    distinct error rather than an empty or partially reconstructed history.
    """

    def __init__(
        self, message: str = "durable task transition history is not available"
    ) -> None:
        super().__init__(message)


@runtime_checkable
class A2ATaskAuthority(Protocol):
    async def dispatch(
        self,
        *,
        message: A2AMessage,
        idempotency_key: str,
        decision: A2ARouteDecision,
        blocking: bool = True,
    ) -> A2ATask: ...

    async def get(self, task_id: str) -> A2ATask | None: ...

    async def list(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[A2ATask], str | None]: ...

    async def cancel(self, task_id: str) -> A2ATask: ...


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class WorkItemA2AAuthority:
    """A2A projection over AU's sole durable WorkItem and dispatch authorities."""

    def __init__(
        self,
        engine_provider: Callable[[], Any],
        *,
        executor: Any = None,
        task_plan_config: Any = None,
        hosted_invocation: Any = None,
    ) -> None:
        self._engine_provider = engine_provider
        self._executor = executor
        # Runs admitted by a non-blocking send (ARBORA-A2A-R001.6). The set
        # holds a reference so a running turn is never garbage collected.
        self._detached: set[asyncio.Task[Any]] = set()
        self.task_plan_config = task_plan_config
        self.hosted_invocation = hosted_invocation

    @staticmethod
    def _session(scope: str) -> Any:
        from arbora.api.contracts.session import resolve_session

        return resolve_session(required_scope=scope)

    def _engine(self) -> tuple[Any, Any]:
        engine = self._engine_provider()
        if engine is None:
            raise RuntimeError("Arbora task authority is unavailable")
        return engine, getattr(engine, "_work_item_engine", engine)

    @staticmethod
    def _owner_ref(session: Any) -> str:
        actor_id = str(getattr(session.actor, "actor_id", "") or "").strip()
        if not actor_id:
            raise PermissionError("Verified A2A task owner is unavailable")
        return persistence_reference(
            "actor", actor_id, namespace=f"a2a-owner:{session.tenant}"
        )

    @staticmethod
    def _task_id(tenant: str, owner_ref: str, key: str) -> str:
        rendered = key.strip()
        if (
            not rendered
            or len(rendered) > 512
            or any(ord(character) < 32 for character in rendered)
        ):
            raise ValueError("A2A idempotency key is invalid")
        return _TASK_PREFIX + _digest(
            {"tenant": tenant, "owner": owner_ref, "key": rendered}
        )

    @staticmethod
    def _work_item_id(task_id: str) -> str:
        if task_id.startswith("work-item:") and len(task_id) <= 512:
            if not any(ord(char) < 32 for char in task_id):
                return task_id
        suffix = task_id.removeprefix(_TASK_PREFIX)
        if not task_id.startswith(_TASK_PREFIX) or len(suffix) != 64:
            raise ValueError("A2A task id is invalid")
        if any(character not in "0123456789abcdef" for character in suffix):
            raise ValueError("A2A task id is invalid")
        return f"{_WORK_ITEM_PREFIX}{task_id}"

    @staticmethod
    def _context_id(
        session: Any, message: A2AMessage, task_id: str, owner_ref: str
    ) -> str:
        supplied = message.context_id or task_id
        digest = _digest(
            {"tenant": session.tenant, "owner": owner_ref, "context": supplied}
        )
        return f"a2a-context-{digest}"

    def _resolve_context_id(
        self, session: Any, message: A2AMessage, task_id: str, owner_ref: str
    ) -> str:
        """Preserve canonical contexts only after durable caller-owned lookup."""
        context_id = message.context_id or ""
        if not context_id.startswith("a2a-context-"):
            return self._context_id(session, message, task_id, owner_ref)
        suffix = context_id.removeprefix("a2a-context-")
        if len(suffix) != 64 or any(char not in "0123456789abcdef" for char in suffix):
            raise ValueError("A2A context id is invalid")
        _engine, work_engine = self._engine()
        match = {
            "a2a_schema": _A2A_METADATA_SCHEMA,
            "a2a_owner_ref": owner_ref,
            "a2a_context_id": context_id,
        }
        cursor = None
        seen: set[str] = set()
        for _ in range(32):
            try:
                page = work_engine.work_items.list(
                    tenant=session.tenant,
                    kind="orchestrator_task",
                    metadata_match=match,
                    cursor=cursor,
                    limit=1,
                )
            except PermissionError:
                raise
            except Exception as exc:
                raise A2AContextUnavailable(
                    "durable A2A context lookup is unavailable"
                ) from exc
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise A2AContextUnavailable("durable A2A context lookup is unavailable")
            rows = page["items"]
            if len(rows) > 1:
                raise A2AContextUnavailable("durable A2A context lookup is unavailable")
            if rows:
                row = rows[0]
                if not (
                    self._owned(row, owner_ref)
                    and row["metadata"].get("a2a_context_id") == context_id
                ):
                    raise PermissionError("A2A context is unavailable")
                if self._session("kg:write") != session:
                    raise PermissionError("A2A caller changed during context lookup")
                return context_id
            cursor = page.get("next_cursor")
            if cursor is None:
                raise PermissionError("A2A context is unavailable")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise A2AContextUnavailable("durable A2A context lookup is unavailable")
            seen.add(cursor)
        raise A2AContextUnavailable(
            "durable A2A context lookup exceeded its page bound"
        )

    @staticmethod
    def _request_digest(
        message: A2AMessage, context_id: str, decision: A2ARouteDecision
    ) -> str:
        return _digest(
            {
                "context_id": context_id,
                "decision": decision.model_dump(mode="json"),
                "message": message.model_dump(mode="json", by_alias=True),
            }
        )

    @staticmethod
    def _metadata(
        *,
        message: A2AMessage,
        context_id: str,
        owner_ref: str,
        request_digest: str,
        decision: A2ARouteDecision,
    ) -> dict[str, Any]:
        return {
            "a2a_schema": _A2A_METADATA_SCHEMA,
            "a2a_context_id": context_id,
            "a2a_message_ref": persistence_reference(
                "message", message.message_id, namespace="arbora-a2a"
            ),
            "a2a_owner_ref": owner_ref,
            "a2a_request_digest": request_digest,
            "a2a_route": decision.model_dump(mode="json", by_alias=True),
        }

    @staticmethod
    def _owned(item: Any, owner_ref: str) -> bool:
        """The engine scopes ``get`` to the verified tenant; the owner is in metadata."""
        metadata = item.get("metadata") if isinstance(item, dict) else None
        return (
            isinstance(item, dict)
            and item.get("kind") == "orchestrator_task"
            and isinstance(metadata, dict)
            and metadata.get("a2a_schema") == _A2A_METADATA_SCHEMA
            and metadata.get("a2a_owner_ref") == owner_ref
        )

    @classmethod
    def _validate_existing(
        cls, item: dict[str, Any], *, session: Any, owner_ref: str, request_digest: str
    ) -> None:
        del session
        if not cls._owned(item, owner_ref):
            raise A2AIdempotencyConflict("A2A idempotency authority conflicts")
        if item["metadata"].get("a2a_request_digest") != request_digest:
            raise A2AIdempotencyConflict(
                "A2A idempotency key was reused with a different request"
            )

    def _submit(
        self,
        work_engine: Any,
        *,
        task_id: str,
        session: Any,
        owner_ref: str,
        description: str,
        metadata: dict[str, Any],
    ) -> tuple[str, bool]:
        from arbora import work_items

        native = self.task_plan_config is not None
        if native:
            from arbora.mcp_server.task_plan import TaskPlanConfig

            if not isinstance(self.task_plan_config, TaskPlanConfig):
                raise TypeError("trusted TaskPlanConfig is required")
            metadata = {**metadata, "a2a_run_id": task_id}
        return work_items.submit_work_item_atomic(
            work_engine,
            kind="orchestrator_task",
            queue="orchestrator_task",
            payload_ref=task_id,
            tenant=session.tenant,
            description=description,
            resource_class="agent_dispatch",
            work_item_id=None if native else f"{_WORK_ITEM_PREFIX}{task_id}",
            run_id=task_id if native else None,
            idempotency_key=task_id,
            created_by=owner_ref,
            metadata=metadata,
        )

    def _agent_executor(self) -> Any:
        if self._executor is None:
            from arbora.agent_runs import AgentRunPort

            self._executor = AgentRunPort()
        return self._executor

    async def _execute_claimed_run(
        self,
        work_engine: Any,
        *,
        item_id: str,
        session: Any,
        claim: Any,
        agent_name: str,
        task: str,
        task_id: str,
        context_id: str,
        run_id: str | None,
    ) -> Any:
        """Execute only inside the host's revocable native owner scope."""
        from arbora import work_items
        from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest

        if self.task_plan_config is not None and self.hosted_invocation is None:
            raise RuntimeError("UPGRADE_REQUIRED: hosted task-plan invocation")
        from arbora.agent_runs import native_run_scope
        from arbora.host_bindings import FLEET_AGENT_NAME, FLEET_TOOLS

        with native_run_scope(
            run_id=run_id or task_id,
            task_id=task_id,
            context_id=context_id,
            work_item_id=item_id,
            session=session,
            claim=claim,
            owner_graph=(
                work_items.native_owner_graph(work_engine)
                if self.task_plan_config is not None
                else None
            ),
            hosted_invocation=self.hosted_invocation,
        ):
            return await self._agent_executor().execute_agent(
                AgentExecutionRequest(
                    agent_name=agent_name,
                    task=task,
                    run_id=run_id or task_id or None,
                    allowed_tools=(
                        FLEET_TOOLS if agent_name == FLEET_AGENT_NAME else None
                    ),
                ),
                session=session,
            )

    async def _run(
        self,
        work_engine: Any,
        *,
        item_id: str,
        session: Any,
        agent_name: str,
        task: str,
        task_id: str = "",
        context_id: str = "",
        run_id: str | None = None,
    ) -> dict[str, int] | None:
        """Lease the WorkItem, run the agent through the harness, commit the outcome.

        The run takes the task id as its run id, so its RunEvents name the
        task. The fleet agent runs with Arbora's own ``find``/``act`` verbs
        as its tools.
        """
        from arbora import work_items

        claim = await asyncio.to_thread(
            work_items.claim_specific,
            work_engine,
            item_id,
            token=f"arbora-a2a:{item_id}",
            lease_ttl_s=_RUN_LEASE_SECONDS,
        )
        if claim is None:
            return None
        outcome, result_ref, error_ref = "succeeded", None, None
        usage = None
        try:
            result = await self._execute_claimed_run(
                work_engine,
                item_id=item_id,
                session=session,
                claim=claim,
                agent_name=agent_name,
                task=task,
                task_id=task_id,
                context_id=context_id,
                run_id=run_id,
            )
            if result.run_id != (run_id or task_id):
                raise RuntimeError("agent result does not belong to this A2A turn")
            result_ref = persistence_reference(
                "run", result.run_id, namespace="arbora-a2a"
            )
            usage = getattr(result, "usage", None)
            remember_task_output(task_id, getattr(result, "output", None))
        except Exception as exc:  # noqa: BLE001 - a failed run is a failed task
            logging.getLogger(__name__).warning(
                "A2A run %s failed: %s: %s", item_id, type(exc).__name__, str(exc)[:300]
            )
            outcome = "failed"
            error_ref = persistence_reference(
                "error", type(exc).__name__, namespace="arbora-a2a"
            )
        await asyncio.to_thread(
            work_items.commit_result,
            work_engine,
            item_id,
            claim,
            outcome=outcome,
            result_ref=result_ref,
            error_ref=error_ref,
        )
        return usage

    def _validate_admitted_item(
        self,
        item: Any,
        *,
        session: Any,
        owner_ref: str,
        request_digest: str,
        run_id: str,
    ) -> dict[str, Any]:
        """Validate both durable caller ownership and the distinct native run ID."""
        if not isinstance(item, dict):
            raise RuntimeError("canonical A2A WorkItem is unavailable after admission")
        self._validate_existing(
            item,
            session=session,
            owner_ref=owner_ref,
            request_digest=request_digest,
        )
        if (
            self.task_plan_config is not None
            and item["metadata"].get("a2a_run_id") != run_id
        ):
            raise A2AIdempotencyConflict("A2A native run identity conflicts")
        return item

    async def dispatch(
        self,
        *,
        message: A2AMessage,
        idempotency_key: str,
        decision: A2ARouteDecision,
        blocking: bool = True,
    ) -> A2ATask:
        if decision.selected_tools:
            # Refuse before durable admission: the current signed AU dispatch
            # carrier cannot enforce this allowlist at execution time.
            raise A2AAssemblyUnavailable(
                "canonical agent dispatch cannot yet enforce an assembled tool subset"
            )
        session = self._session("kg:write")
        _engine, work_engine = self._engine()
        owner_ref = self._owner_ref(session)
        task_id = self._task_id(session.tenant, owner_ref, idempotency_key)
        context_id = await asyncio.to_thread(
            self._resolve_context_id, session, message, task_id, owner_ref
        )
        request_digest = self._request_digest(message, context_id, decision)
        metadata = self._metadata(
            message=message,
            context_id=context_id,
            owner_ref=owner_ref,
            request_digest=request_digest,
            decision=decision,
        )
        from arbora.access.task_guard import screen_and_redact_agent_task

        description = screen_and_redact_agent_task(message.task_text())
        item_id, created = await asyncio.to_thread(
            self._submit,
            work_engine,
            task_id=task_id,
            session=session,
            owner_ref=owner_ref,
            description=description,
            metadata=metadata,
        )
        run_id = task_id
        if self.task_plan_config is not None:
            task_id = item_id
        if item_id != self._work_item_id(task_id):
            raise RuntimeError("canonical A2A WorkItem returned an invalid identity")

        from arbora import work_items

        item = await asyncio.to_thread(work_items.get_work_item, work_engine, item_id)
        item = self._validate_admitted_item(
            item,
            session=session,
            owner_ref=owner_ref,
            request_digest=request_digest,
            run_id=run_id,
        )
        usage = None
        if not blocking and (created or item.get("status") not in _TERMINAL):
            # ARBORA-A2A-R001.6: answer with the admitted task now. The run
            # continues under that task; the caller follows it with
            # ``tasks/get``. The detached run inherits the caller's session
            # through the copied context.
            self._detach(
                self._run(
                    work_engine,
                    item_id=item_id,
                    session=session,
                    agent_name=decision.agent_name,
                    task=description,
                    task_id=task_id,
                    context_id=context_id,
                    run_id=run_id,
                ),
                item_id,
            )
        elif created or item.get("status") not in _TERMINAL:
            usage = await self._run(
                work_engine,
                item_id=item_id,
                session=session,
                agent_name=decision.agent_name,
                task=description,
                task_id=task_id,
                context_id=context_id,
                run_id=run_id,
            )
            refreshed = await asyncio.to_thread(
                work_items.get_work_item, work_engine, item_id
            )
            if isinstance(refreshed, dict):
                item = refreshed
        task = self._project(item, task_id=task_id)
        if usage is not None:
            task.metadata["arbora"]["usage"] = {
                "runId": task.metadata["arbora"]["runId"],
                "taskId": task.id,
                "contextId": context_id,
                "scope": "turn",
                "counts": dict(usage),
            }
        return task

    def _detach(self, run: Any, item_id: str) -> None:
        """Keep one non-blocking run alive until it commits its outcome."""

        def _done(finished: asyncio.Task[Any]) -> None:
            self._detached.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logging.getLogger(__name__).warning(
                    "A2A detached run %s failed: %s",
                    item_id,
                    type(finished.exception()).__name__,
                )

        task = asyncio.get_running_loop().create_task(run)
        self._detached.add(task)
        task.add_done_callback(_done)

    def _authorized_item(self, task_id: str, scope: str) -> dict[str, Any] | None:
        from arbora import work_items

        session = self._session(scope)
        _engine, work_engine = self._engine()
        item = work_items.get_work_item(work_engine, self._work_item_id(task_id))
        return item if self._owned(item, self._owner_ref(session)) else None

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
        rendered = str(value).strip()
        return rendered or None

    @staticmethod
    def _project(item: dict[str, Any], *, task_id: str) -> A2ATask:
        raw_state = str(item.get("status") or "")
        state = _STATE_MAP.get(raw_state)
        if state is None:
            raise RuntimeError("canonical WorkItem has an invalid A2A state")
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            raise RuntimeError("canonical WorkItem lacks A2A metadata")
        route = metadata.get("a2a_route")
        public_route = route if isinstance(route, dict) else {}
        run_id = str(metadata.get("a2a_run_id") or task_id)
        public_metadata: dict[str, Any] = {
            "taskAuthorityRef": str(
                item.get("work_item_id") or WorkItemA2AAuthority._work_item_id(task_id)
            ),
            "runId": run_id,
            "routing": public_route,
        }
        from arbora.observability.trace_ontology import (
            trace_id as canonical_trace_id,
        )

        public_metadata["runTraceRef"] = canonical_trace_id(run_id)
        return A2ATask(
            id=task_id,
            context_id=str(metadata.get("a2a_context_id") or ""),
            status=A2ATaskStatus(
                state=state,
                timestamp=WorkItemA2AAuthority._timestamp(
                    item["updated_at_ms"] / 1000
                    if isinstance(item.get("updated_at_ms"), (int, float))
                    else item.get("updated_at")
                ),
            ),
            artifacts=task_output_artifacts(task_id),
            metadata={"arbora": public_metadata},
        )

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
        WorkItemA2AAuthority._check_reply_marker(event, task_id)
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
            WorkItemA2AAuthority._raise_reply_marker(event, error)

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
        if item.get("status") != "succeeded":
            return None
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
            return None
        if not isinstance(rows, list) or len(rows) != 1:
            raise RuntimeError("durable A2A reply is unavailable")
        row = rows[0]
        payload = row.get("payload") if isinstance(row, dict) else None
        event = self._read_reply_payload(payload, run_id)
        text = self._reply_text(event, run_id)
        if len(text) > _MAX_OUTPUT_CHARS:
            raise _ReplyUnavailable("reply_too_large")
        if not text:
            return None
        return [
            A2AArtifact(
                artifact_id=f"{task_id}:reply",
                name="reply",
                parts=[A2ATextPart(text=text)],
            )
        ]

    async def get_committed_task(self, task_id: str) -> A2ATask | None:
        return await self.get(task_id, include_committed_event=True)

    async def get(
        self, task_id: str, *, include_committed_event: bool = False
    ) -> A2ATask | None:
        read_session = self._session("kg:read")
        original = self._session("work:read") if include_committed_event else None
        item = await asyncio.to_thread(self._authorized_item, task_id, "kg:read")
        if item is None:
            return None
        if original is not None:
            current = self._session("work:read")
            owner_ref = self._owner_ref(original)
            if (
                current != original
                or item.get("metadata", {}).get("a2a_owner_ref") != owner_ref
            ):
                raise PermissionError("A2A caller changed during task read")
        if self._session("kg:read") != read_session:
            raise PermissionError("A2A caller changed during task read")
        task = self._project(item, task_id=task_id)
        try:
            task.artifacts = await asyncio.to_thread(
                self._persisted_artifacts, task_id, item=item
            )
        except _ReplyUnavailable as exc:
            task.artifacts = None
            task.metadata["arbora"]["reply"] = exc.availability.model_dump(mode="json")
        if self._session("kg:read") != read_session:
            raise PermissionError("A2A caller changed during reply read")
        if include_committed_event:
            await self._attach_terminal_event(task, task_id, original)
        return task

    async def _attach_terminal_event(
        self, task: A2ATask, task_id: str, session: Any
    ) -> None:
        """Read and attach a terminal event under the unchanged caller authority."""
        from .terminal_event import (
            A2ATerminalEventUnavailable,
            read_terminal_event,
        )

        owner_ref = self._owner_ref(session)
        engine, _work_engine = self._engine()
        try:
            event = await asyncio.wait_for(
                read_terminal_event(
                    engine, session=session, task_id=task_id, owner_ref=owner_ref
                ),
                timeout=5.0,
            )
        except TimeoutError as exc:
            raise A2ATerminalEventUnavailable(
                "committed terminal event is unavailable"
            ) from exc
        current = self._session("work:read")
        if (
            current != session
            or current.tenant != session.tenant
            or current.graph != session.graph
            or self._owner_ref(current) != owner_ref
        ):
            raise PermissionError("A2A caller changed during terminal read")
        if event["status"] == "available" and (
            _STATE_MAP[event["outcome"]] != task.status.state
        ):
            raise A2ATerminalEventUnavailable("committed terminal event is unavailable")
        task.metadata["arbora"]["terminalEvent"] = event

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

    def _list_rows(
        self, *, cursor: str | None, limit: int, tenant: str, owner_ref: str
    ) -> Any:
        _engine, work_engine = self._engine()
        return work_engine.work_items.list(
            tenant=tenant,
            kind="orchestrator_task",
            metadata_match={
                "a2a_schema": _A2A_METADATA_SCHEMA,
                "a2a_owner_ref": owner_ref,
            },
            cursor=cursor,
            limit=limit,
        )

    @classmethod
    def _project_list_row(cls, row: Any, *, owner_ref: str) -> A2ATask:
        if not cls._owned(row, owner_ref):
            raise RuntimeError("canonical WorkItem list crossed its authority scope")
        item_id = str(row.get("work_item_id") or "")
        task_id = (
            item_id
            if row["metadata"].get("a2a_run_id")
            else item_id.removeprefix(_WORK_ITEM_PREFIX)
        )
        if cls._work_item_id(task_id) != item_id:
            raise RuntimeError("canonical WorkItem list returned an invalid identity")
        return cls._project(row, task_id=task_id)

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

    async def list(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[A2ATask], str | None]:
        if not 1 <= limit <= 100:
            raise ValueError("A2A task page limit must be between 1 and 100")
        session = self._session("kg:read")
        owner_ref = self._owner_ref(session)
        native_cursor = self._decode_cursor(
            cursor, owner_ref=owner_ref, tenant=session.tenant
        )
        page = await asyncio.to_thread(
            self._list_rows,
            cursor=native_cursor,
            limit=limit,
            tenant=session.tenant,
            owner_ref=owner_ref,
        )
        if self._session("kg:read") != session:
            raise PermissionError("A2A caller changed during task listing")
        rows, continuation = self._validated_list_page(page, limit, native_cursor)
        tasks = [self._project_list_row(row, owner_ref=owner_ref) for row in rows]
        next_cursor = (
            self._cursor(owner_ref, session.tenant, continuation)
            if continuation is not None
            else None
        )
        return tasks, next_cursor

    async def cancel(self, task_id: str) -> A2ATask:
        item = await asyncio.to_thread(self._authorized_item, task_id, "kg:write")
        if item is None:
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        if item.get("status") == "cancelled":
            return self._project(item, task_id=task_id)
        if item.get("status") in _TERMINAL:
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        from arbora import work_items

        _engine, work_engine = self._engine()
        item_id = self._work_item_id(task_id)
        if not await asyncio.to_thread(
            work_items.cancel_work_item, work_engine, item_id
        ):
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        current = await asyncio.to_thread(
            work_items.get_work_item, work_engine, item_id
        )
        if not isinstance(current, dict):
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        return self._project(current, task_id=task_id)
