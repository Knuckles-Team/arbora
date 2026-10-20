"""Canonical WorkItem/RunTrace authority used by every unary A2A surface."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from agent_connector_sdk.privacy import persistence_reference

from .models import (
    A2AArtifact,
    A2AMessage,
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
#: bounded process-local view serves the reply on task reads.
_OUTPUTS: OrderedDict[str, str] = OrderedDict()
_MAX_OUTPUTS = 1024
_MAX_OUTPUT_CHARS = 65_536


def remember_task_output(task_id: str, output: Any) -> None:
    """Keep the reply text of one finished run for its task reads."""
    if not task_id or output in (None, ""):
        return
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    _OUTPUTS[task_id] = text[:_MAX_OUTPUT_CHARS]
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
        self, engine_provider: Callable[[], Any], *, executor: Any = None
    ) -> None:
        self._engine_provider = engine_provider
        self._executor = executor
        self._running: set[asyncio.Task[Any]] = set()

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
        if task_id.startswith("work-item:"):
            suffix = task_id.removeprefix("work-item:")
            if len(suffix) == 64 and all(c in "0123456789abcdef" for c in suffix):
                return task_id
            raise ValueError("A2A task id is invalid")
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

    @staticmethod
    def _submit(
        work_engine: Any,
        *,
        task_id: str,
        session: Any,
        owner_ref: str,
        description: str,
        metadata: dict[str, Any],
    ) -> tuple[str, bool]:
        from arbora import work_items

        native = work_items.supports_run_admission()
        if native:
            legacy_id = f"{_WORK_ITEM_PREFIX}{task_id}"
            legacy = work_items.get_work_item(work_engine, legacy_id)
            if legacy is not None:
                WorkItemA2AAuthority._validate_existing(
                    legacy,
                    session=session,
                    owner_ref=owner_ref,
                    request_digest=metadata["a2a_request_digest"],
                )
                return legacy_id, False
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

    async def _run(
        self,
        work_engine: Any,
        *,
        item_id: str,
        session: Any,
        agent_name: str,
        task: str,
        task_id: str = "",
        run_id: str | None = None,
        context_id: str | None = None,
        owner_ref: str | None = None,
        history_required: bool = False,
        usage_context_id: str | None = None,
        streaming: bool = False,
    ) -> tuple[dict[str, int] | None, dict[str, Any] | None] | None:
        """Lease the WorkItem, run the agent through the harness, commit the outcome.

        Native tasks carry the admitted run id separately from their opaque
        WorkItem task id. The fleet agent runs with Arbora's own ``find``/``act`` verbs
        as its tools.
        """
        from arbora import work_items
        from arbora.api.contracts.agent_control_contracts import (
            AgentExecutionRequest,
        )

        streaming = await self._events_enabled(run_id, streaming)
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
        metrics = None
        commit_options: dict[str, Any] = {}
        try:
            from arbora.admitted_run import bind_admitted_claim
            from arbora.host_bindings import FLEET_AGENT_NAME, FLEET_TOOLS

            binding = (
                bind_admitted_claim(
                    work_engine,
                    session=session,
                    run_id=run_id,
                    work_item_id=item_id,
                    claim=claim,
                    context_id=context_id,
                    owner_ref=owner_ref,
                    history_required=history_required,
                )
                if run_id is not None
                else contextlib.nullcontext()
            )
            from arbora.run_events import terminal_event_scope

            with binding as admitted:
                async with terminal_event_scope(
                    admitted, enabled=streaming, commit_options=commit_options
                ):
                    result = await self._agent_executor().execute_agent(
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
            if result.run_id != (run_id or task_id):
                raise RuntimeError("agent result does not belong to this A2A turn")
            result_ref = persistence_reference(
                "run", result.run_id, namespace="arbora-a2a"
            )
            usage = getattr(result, "usage", None)
            raw_metrics = getattr(result, "metrics", None)
            if raw_metrics is not None:
                from arbora.api.contracts.agent_control_contracts import RunMetrics

                metrics = RunMetrics.model_validate(raw_metrics).model_dump(
                    mode="json", exclude_none=True
                )
            remember_task_output(task_id, getattr(result, "output", None))
        except Exception as exc:  # noqa: BLE001 - a failed run is a failed task
            logging.getLogger(__name__).warning(
                "A2A run %s failed: %s: %s", item_id, type(exc).__name__, str(exc)[:300]
            )
            outcome = "failed"
            error_ref = persistence_reference(
                "error", type(exc).__name__, namespace="arbora-a2a"
            )
        usage_observation = None
        if outcome == "succeeded" and usage and usage_context_id is not None:
            usage_observation = {
                "run_id": run_id or task_id,
                "context_id": usage_context_id,
                "counts": dict(usage),
            }
        await asyncio.to_thread(
            work_items.commit_result,
            work_engine,
            item_id,
            claim,
            outcome=outcome,
            result_ref=result_ref,
            error_ref=error_ref,
            usage_observation=usage_observation,
            **commit_options,
        )
        return (usage, metrics) if outcome == "succeeded" else None

    async def dispatch(
        self,
        *,
        message: A2AMessage,
        idempotency_key: str,
        decision: A2ARouteDecision,
        streaming: bool = False,
    ) -> A2ATask:
        if streaming and not await self.streaming_supported():
            raise A2AStreamingUnavailable("durable streaming is unavailable")
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
        if item_id.startswith("work-item:"):
            task_id = item_id
        if item_id != self._work_item_id(task_id):
            raise RuntimeError("canonical A2A WorkItem returned an invalid identity")

        from arbora import work_items

        item = await asyncio.to_thread(work_items.get_work_item, work_engine, item_id)
        if not isinstance(item, dict):
            raise RuntimeError("canonical A2A WorkItem is unavailable after admission")
        self._validate_existing(
            item,
            session=session,
            owner_ref=owner_ref,
            request_digest=request_digest,
        )
        return await self._start_or_replay(
            work_engine,
            item,
            created=created,
            streaming=streaming,
            arguments={
                "item_id": item_id,
                "usage_context_id": context_id
                if item_id.startswith("work-item:")
                else None,
                "session": session,
                "agent_name": decision.agent_name,
                "task": description,
                "task_id": task_id,
                "run_id": run_id if task_id.startswith("work-item:") else None,
                "context_id": context_id,
                "owner_ref": owner_ref,
                "history_required": bool(
                    message.context_id and message.context_id.startswith("a2a-context-")
                ),
            },
        )

    async def _start_or_replay(
        self,
        work_engine: Any,
        item: dict[str, Any],
        *,
        created: bool,
        streaming: bool,
        arguments: dict[str, Any],
    ) -> A2ATask:
        from arbora import work_items

        measurements = None
        if created or (not streaming and item.get("status") not in _TERMINAL):
            execution = self._run(work_engine, streaming=streaming, **arguments)
            if streaming:
                worker = asyncio.create_task(execution)
                self._running.add(worker)
                worker.add_done_callback(self._execution_done)
            else:
                measurements = await execution
            refreshed = await asyncio.to_thread(
                work_items.get_work_item, work_engine, arguments["item_id"]
            )
            if isinstance(refreshed, dict):
                item = refreshed
        task = self._project(item, task_id=arguments["task_id"])
        usage, metrics = measurements or (None, None)
        if usage is not None:
            task.metadata["arbora"]["usage"] = {
                "runId": task.metadata["arbora"]["runId"],
                "taskId": task.id,
                "contextId": arguments["context_id"],
                "scope": "turn",
                "counts": dict(usage),
            }
        if metrics is not None:
            task.metadata["arbora"]["metrics"] = {
                "runId": task.metadata["arbora"]["runId"],
                "taskId": task.id,
                "contextId": arguments["context_id"],
                "scope": "turn",
                "measurements": metrics,
            }
        return task

    def _execution_done(self, task: asyncio.Task[Any]) -> None:
        self._running.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).warning(
                "durable A2A execution ended without a confirmed terminal commit"
            )

    def _event_client(self) -> Any:
        from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

        _engine, work_engine = self._engine()
        return work_engine.graph_compute.for_graph(
            CONTROL_GRAPH
        ).async_client.work_items

    async def _events_enabled(self, run_id: str | None, requested: bool) -> bool:
        return requested or (run_id is not None and await self.streaming_supported())

    async def streaming_supported(self) -> bool:
        from agent_utilities import host_ports

        from arbora import work_items
        from arbora.run_events import events_supported

        if not work_items.supports_run_admission():
            return False
        if not callable(
            getattr(host_ports, "register_durable_run_event_factory", None)
        ):
            return False
        return await events_supported(self._event_client())

    async def read_events(self, task_id: str, cursor: str | None) -> dict[str, Any]:
        from arbora.run_events import RunEventsUnavailable, validate_page

        session = self._session("kg:read")
        item = await asyncio.to_thread(self._authorized_item, task_id, "kg:read")
        if item is None:
            raise PermissionError("task is unavailable")
        run_id = item["metadata"].get("a2a_run_id")
        if not isinstance(run_id, str):
            raise A2AStreamingUnavailable("durable streaming is unavailable")
        try:
            page = await asyncio.wait_for(
                self._event_client().get_events(
                    tenant=session.tenant,
                    work_item_id=item["work_item_id"],
                    run_id=run_id,
                    after_cursor=cursor,
                    limit=128,
                ),
                timeout=5.0,
            )
        except PermissionError:
            raise
        except Exception as exc:
            raise RunEventsUnavailable("durable run events are unavailable") from exc
        session.engine_verified_context()
        return validate_page(
            page,
            work_item_id=item["work_item_id"],
            run_id=run_id,
            owner_ref=self._owner_ref(session),
        )

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
        public_metadata: dict[str, Any] = {
            "taskAuthorityRef": WorkItemA2AAuthority._work_item_id(task_id),
            "runId": metadata.get("a2a_run_id", task_id),
            "routing": public_route,
        }
        from arbora.observability.trace_ontology import (
            trace_id as canonical_trace_id,
        )

        public_metadata["runTraceRef"] = canonical_trace_id(public_metadata["runId"])
        return A2ATask(
            id=task_id,
            context_id=str(metadata.get("a2a_context_id") or ""),
            status=A2ATaskStatus(
                state=state,
                timestamp=WorkItemA2AAuthority._timestamp(
                    item["updated_at_ms"] / 1000
                    if "updated_at_ms" in item
                    else item.get("updated_at")
                ),
            ),
            artifacts=task_output_artifacts(task_id),
            metadata={"arbora": public_metadata},
        )

    async def replay_terminal(
        self, task_id: str, cursor: str | None
    ) -> tuple[A2ATask | None, str]:
        """Replay only a committed terminal event; never admit or execute work."""
        from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

        from .terminal_event import A2ATerminalEventUnavailable
        from .terminal_replay import project_replay

        session = self._session("work:read")
        owner_ref = self._owner_ref(session)
        item = await asyncio.to_thread(self._authorized_item, task_id, "kg:read")
        if item is None:
            raise PermissionError("A2A task unavailable")
        task = self._project(item, task_id=task_id)
        engine, _ = self._engine()
        try:
            client = engine.graph_compute.for_graph(CONTROL_GRAPH).async_client
            value = await asyncio.wait_for(
                client.work_items.replay_terminal(
                    tenant=session.tenant,
                    work_item_id=self._work_item_id(task_id),
                    after_cursor=cursor,
                ),
                timeout=5.0,
            )
        except (PermissionError, ValueError):
            raise
        except Exception as exc:
            raise A2ATerminalEventUnavailable("terminal replay is unavailable") from exc
        if self._session("work:read") != session:
            raise PermissionError("A2A caller changed during terminal replay")
        replay_item, event, next_cursor = project_replay(
            value,
            session=session,
            task_id=task_id,
            owner_ref=owner_ref,
            work_item_id=self._work_item_id(task_id),
            run_id=task.metadata["arbora"]["runId"],
            after_cursor=cursor,
        )
        if event is None:
            return None, next_cursor
        result = self._project(replay_item, task_id=task_id)
        result.metadata["arbora"]["terminalEvent"] = event
        return result, next_cursor

    async def get_committed_task(self, task_id: str) -> A2ATask | None:
        return await self.get(task_id, include_committed_event=True)

    async def get(
        self, task_id: str, *, include_committed_event: bool = False
    ) -> A2ATask | None:
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
        task = self._project(item, task_id=task_id)
        if include_committed_event:
            from .terminal_event import (
                A2ATerminalEventUnavailable,
                read_terminal_event,
            )

            session = original
            owner_ref = self._owner_ref(session)
            engine, _work_engine = self._engine()
            try:
                event = await asyncio.wait_for(
                    read_terminal_event(
                        engine,
                        session=session,
                        task_id=task_id,
                        owner_ref=owner_ref,
                        work_item_id=self._work_item_id(task_id),
                        run_id=task.metadata["arbora"]["runId"],
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
                raise A2ATerminalEventUnavailable(
                    "committed terminal event is unavailable"
                )
            task.metadata["arbora"]["terminalEvent"] = event
        return task

    @staticmethod
    def _cursor(owner_ref: str, tenant: str, cursor: str) -> str:
        return f"{_digest({'tenant': tenant, 'owner': owner_ref, 'cursor': cursor})}.{cursor}"

    @classmethod
    def _decode_cursor(
        cls, cursor: str | None, *, owner_ref: str, tenant: str
    ) -> str | None:
        if cursor is None:
            return None
        digest, separator, native = cursor.partition(".")
        if not separator or digest != _digest(
            {"tenant": tenant, "owner": owner_ref, "cursor": native}
        ):
            raise ValueError("A2A task cursor is invalid")
        return native

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
        _engine, work_engine = self._engine()
        page = await asyncio.to_thread(
            work_engine.work_items.list,
            tenant=session.tenant,
            cursor=native_cursor,
            limit=limit,
            kind="orchestrator_task",
            metadata_match={
                "a2a_schema": _A2A_METADATA_SCHEMA,
                "a2a_owner_ref": owner_ref,
            },
        )
        if self._session("kg:read") is not session:
            raise PermissionError("A2A task list caller changed during read")
        session.engine_verified_context()
        if (
            not isinstance(page, dict)
            or not isinstance(page.get("items"), list)
            or len(page["items"]) > limit
        ):
            raise RuntimeError("canonical WorkItem list returned invalid data")
        tasks = []
        for row in page["items"]:
            if not self._owned(row, owner_ref):
                raise RuntimeError(
                    "canonical WorkItem list crossed its authority scope"
                )
            item_id = row["work_item_id"]
            task_id = item_id.removeprefix(_WORK_ITEM_PREFIX)
            if self._work_item_id(task_id) != item_id:
                raise RuntimeError("canonical WorkItem list returned invalid identity")
            tasks.append(self._project(row, task_id=task_id))
        next_cursor = page.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise RuntimeError("canonical WorkItem cursor is invalid")
        return tasks, self._cursor(
            owner_ref, session.tenant, next_cursor
        ) if next_cursor else None

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
