"""Canonical WorkItem/RunTrace authority used by every unary A2A surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from scion_sdk.privacy import persistence_reference

from arbora.api.domain.a2a.models import (
    A2AArtifact,
    A2AMessage,
    A2ARouteDecision,
    A2ATask,
    A2ATaskState,
    A2ATextPart,
)
from arbora.core.a2a_errors import A2AIdempotencyConflict
from arbora.core.settings import RuntimeConfig
from arbora.core.work_item_errors import NativeWorkItemRequired
from arbora.run_origin import RunOriginRegistry

from .held_output import held_metadata
from .run_lifecycle import (
    _MAX_OUTPUT_CHARS,
    _TERMINAL,
    _WORK_ITEM_PREFIX,
    RunLifecycle,
)
from .task_projection import (
    _STATE_MAP,
    TaskProjection,
    _digest,
    _ReplyUnavailable,
)
from .tool_subset import require_carrier as require_tool_subset_carrier

logger = logging.getLogger(__name__)


__all__ = [
    "A2AContextUnavailable",
    "A2AStreamingUnavailable",
    "A2ATransitionHistoryUnavailable",
    "A2ATaskAuthority",
    "A2ATaskNotCancelable",
    "WorkItemA2AAuthority",
]

_TASK_PREFIX = "a2a-"
_A2A_METADATA_SCHEMA = "arbora-a2a-unary-v1"
#: The most content one child message carries (ARBORA-RELEASE-R003.4.12).
CHILD_INPUT_MAX_BYTES = 16_384
_MAX_OUTPUTS = 1024


class TaskOutputs:
    """Agent reply text of recent runs, by task id (owned by one authority).

    A WorkItem holds only the result reference; the durable record is the run's
    RunEvents in EG. This bounded view serves immediate dispatch/list projections;
    tasks/get reads the durable record rather than trusting it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._texts: OrderedDict[str, str] = OrderedDict()

    def remember(self, task_id: str, output: Any) -> None:
        """Keep the reply text of one finished run for its task reads."""
        if not task_id or output in (None, ""):
            return
        text = output if isinstance(output, str) else json.dumps(output, default=str)
        with self._lock:
            if len(text) > _MAX_OUTPUT_CHARS:
                self._texts.pop(task_id, None)
                return
            self._texts[task_id] = text
            self._texts.move_to_end(task_id)
            while len(self._texts) > _MAX_OUTPUTS:
                self._texts.popitem(last=False)

    def forget(self, task_id: str) -> None:
        """Drop the reply text of ``task_id``."""
        with self._lock:
            self._texts.pop(task_id, None)

    def artifacts(self, task_id: str) -> list[A2AArtifact] | None:
        """The agent's reply to ``task_id`` as the task's one artifact, when known."""
        with self._lock:
            text = self._texts.get(task_id)
        if not text:
            return None
        return [
            A2AArtifact(
                artifact_id=f"{task_id}:reply",
                name="reply",
                parts=[A2ATextPart(text=text)],
            )
        ]


class A2AContextUnavailable(RuntimeError):
    """The durable context ownership reader could not supply a bounded answer."""


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
        model_profile: Any = None,
        media: dict[str, Any] | None = None,
        harness: Any = None,
    ) -> A2ATask: ...

    async def get(self, task_id: str) -> A2ATask | None: ...

    async def list(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[A2ATask], str | None]: ...

    async def cancel(self, task_id: str) -> A2ATask: ...


def _opening_session(message: A2AMessage) -> dict[str, str]:
    """The opening session keys of ``agents.run``, copied and no other message metadata.

    ``client_session_id`` and ``client_principal`` become ``a2a_client_session_id`` and
    ``a2a_client_principal`` when each is non-empty text of at most 256 characters
    (ARBORA-IDENTITY-R026.3).
    """
    found: dict[str, str] = {}
    for key in ("client_session_id", "client_principal"):
        value = message.metadata.get(key)
        if isinstance(value, str) and 0 < len(value) <= 256:
            found[f"a2a_{key}"] = value
    return found


def harness_metadata(harness: Any) -> dict[str, Any]:
    """The admitted harness decision a run reads by reference (ARBORA-A2A-R018.1)."""
    return {
        "decision_record_ref": harness.decision_record_ref,
        "harness_config_digest": harness.harness_config_digest,
        "allowed_tool_subset": list(harness.admitted_tools),
        "context_budget_tokens": harness.context_budget_tokens,
    }


class WorkItemA2AAuthority:
    """A2A projection over AU's sole durable WorkItem and dispatch authorities."""

    def __init__(
        self,
        engine_provider: Callable[[], Any],
        *,
        origins: RunOriginRegistry,
        executor: Any = None,
        task_plan_config: Any = None,
        hosted_invocation: Any = None,
        events: Any = None,
        delegations: Any = None,
        delegation_reader: Any = None,
        clock: Callable[[], float] = time.time,
        held_output_refs: Callable[[str, Any], Any] | None = None,
        ports: Any = None,
        config: RuntimeConfig,
    ) -> None:
        self._engine_provider = engine_provider
        #: The recent run replies this authority serves to its projections.
        self.outputs = TaskOutputs()
        #: The host settings the default agent executor reads.
        self._config = config
        #: The host's ``HostPortsSlot`` the default agent executor runs on.
        self._ports = ports
        #: ``(task_id, session) -> HeldOutputRefs | None``: the refs of the held
        #: output of one run (ARBORA-A2A-R022). ``None`` sets no held output, and the
        #: run then carries no block, so the critique gate holds its output.
        self._held_output_refs = held_output_refs
        #: Produces the held-output refs of a finished run from engine methods
        #: (ARBORA-A2A-R022.1); ``None`` produces none and every output stays held.
        self.held_output_source: Any = None
        #: The graph the critique is recorded under; set with the source.
        self.held_output_graph: str = ""
        #: Records the run's ``agent_run`` delegation at admission
        #: (``ARBORA-A2A-R001.2.2``); ``None`` admits a run without one.
        self.delegations = delegations
        #: Reads a delegation by id so a reclaimed run proves its owner
        #: (``ARBORA-A2A-R001.2.2``); ``None`` refuses every reclaim.
        self.delegation_reader = delegation_reader
        self.clock = clock
        #: The durable event publisher (ARBORA-A2A-R001.2); ``None`` publishes
        #: nothing and leaves streaming unserved.
        self.events = events
        #: The push-notification worker (ARBORA-A2A-R013); ``None`` pushes nothing.
        self.push: Any = None
        #: The provisioning service (ARBORA-FLEET-R024.6, R027.3); ``None`` provisions nothing.
        self.provisioning: Any = None
        self._executor = executor
        self._origins = origins
        # Runs admitted by a non-blocking send (ARBORA-A2A-R001.6). The set
        # holds a reference so a running turn is never garbage collected.
        self._detached: set[asyncio.Task[Any]] = set()
        self.task_plan_config = task_plan_config
        self.hosted_invocation = hosted_invocation
        #: Writes the team records and applies the coordination decision at admission
        #: (ARBORA-OPS-R877); ``None`` until the host binds one.
        self.team: Any = None
        self.runs = RunLifecycle(
            engine=lambda *a, **k: self._engine(*a, **k),
            executor=lambda: self._executor,
            held_output_refs=self._held_output_refs,
            clock=lambda: self.clock,
            delegation_reader=lambda: self.delegation_reader,
            events=lambda: self.events,
            held_output_graph=lambda: self.held_output_graph,
            held_output_source=lambda: self.held_output_source,
            hosted_invocation=lambda: self.hosted_invocation,
            outputs=self.outputs,
            push=lambda: self.push,
            task_plan_config=lambda: self.task_plan_config,
        )
        self.projection = TaskProjection(
            work_item_id=lambda *a, **k: self._work_item_id(*a, **k),
            engine=lambda *a, **k: self._engine(*a, **k),
            held_output_source=lambda: self.held_output_source,
            outputs=self.outputs,
        )

    def provision_capacity(self, request: Any) -> Any:
        """Provision instances for a run before its dispatch (ARBORA-FLEET-R024.6)."""
        if self.provisioning is None:
            raise RuntimeError("Arbora provisioning is unavailable")
        return self.provisioning.provision(request)

    def provision_work_item(self, view: Any, request: Any, **kwargs: Any) -> Any:
        """Discover, distribute and provision one work item (ARBORA-FLEET-R027.3)."""
        if self.provisioning is None:
            raise RuntimeError("Arbora provisioning is unavailable")
        return self.provisioning.for_work_item(view, request, **kwargs)

    @staticmethod
    def _session(scope: str) -> Any:
        from arbora.security.session import resolve_session

        return resolve_session(required_scope=scope)

    def engine(self) -> Any:
        """The served engine: the tool-task host reads its blobs and WorkItems here."""
        return self._engine()[0]

    def work_engine(self) -> Any:
        """The engine's WorkItem surface the host reclaim consumer lists and claims through."""
        return self._engine()[1]

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
        from arbora.a2a.source_grant import bound_source_grant
        from arbora.agent_runs import native_stream_protocol

        protocol = native_stream_protocol()
        grant = bound_source_grant()
        return _digest(
            {
                **({"source_grant": grant} if grant is not None else {}),
                **({"host_transport_protocol": protocol} if protocol else {}),
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
        model_profile: Any = None,
    ) -> dict[str, Any]:
        from arbora.a2a.source_grant import work_item_grant_metadata
        from arbora.agent_runs import native_stream_protocol

        protocol = native_stream_protocol()
        return {
            # Only the grant the verified admission bound; never message text.
            **work_item_grant_metadata(),
            **(
                {
                    "a2a_model_profile": model_profile.profile,
                    "a2a_preference_honoured": model_profile.preference_honoured,
                }
                if model_profile is not None
                else {}
            ),
            **({"host_transport_protocol": protocol} if protocol else {}),
            "a2a_schema": _A2A_METADATA_SCHEMA,
            "a2a_context_id": context_id,
            "a2a_message_ref": persistence_reference(
                "message", message.message_id, namespace="arbora-a2a"
            ),
            "a2a_owner_ref": owner_ref,
            "a2a_request_digest": request_digest,
            "a2a_route": decision.model_dump(mode="json", by_alias=True),
            **_opening_session(message),
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
            from arbora.core.task_plan_config import TaskPlanConfig

            if not isinstance(self.task_plan_config, TaskPlanConfig):
                raise TypeError("trusted TaskPlanConfig is required")
            metadata = {**metadata, "a2a_run_id": task_id}
        from arbora.api.invoke.pipeline import OperationRefused

        try:
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
        except OperationRefused as exc:
            if exc.code == "IDEMPOTENCY_CONFLICT":
                raise A2AIdempotencyConflict(
                    "idempotency key conflicts with admission"
                ) from exc
            raise

    async def _with_delegation(
        self,
        metadata: dict[str, Any],
        work_engine: Any,
        *,
        session: Any,
        task_id: str,
        agent_name: str,
    ) -> dict[str, Any]:
        """Name the run's ``agent_run`` delegation in the item metadata.

        The delegation is recorded once, at first admission: a retried admission
        finds the item and so never re-records (which could undo a revocation).
        """
        if self.delegations is None:
            return metadata
        from arbora import work_items
        from arbora.api.ops.pin_guards import refusal
        from arbora.pin_lag.medulla import PinLagUnavailable

        from .run_delegation import DELEGATION_KEY, run_delegation_id

        existing = await asyncio.to_thread(
            work_items.get_work_item, work_engine, self._work_item_id(task_id)
        )
        if existing is None:
            from arbora.core.agent_names import preloaded_agent_tools

            try:
                await self.delegations.record(
                    tenant=session.tenant,
                    principal=str(session.actor.actor_id),
                    task_id=task_id,
                    run_id=task_id,
                    operations=(
                        f"agent:{agent_name}",
                        *(preloaded_agent_tools(agent_name) or ()),
                    ),
                )
            except PinLagUnavailable as missing:
                raise refusal(missing) from missing
        return {**metadata, DELEGATION_KEY: run_delegation_id(task_id)}

    async def owned_item(self, task_id: str) -> dict[str, Any] | None:
        """The caller's own task record, or ``None`` for an absent or foreign task."""
        try:
            return await asyncio.to_thread(self._authorized_item, task_id, "kg:read")
        except ValueError:
            return None

    async def resume_reclaimed(
        self,
        work_engine: Any,
        item: dict[str, Any],
        claim: dict[str, Any],
        *,
        session: Any,
    ) -> dict[str, int] | None:
        """Resume a run whose lease expired, under the claim the consumer won."""
        return await self.runs.resume_reclaimed(
            work_engine, item, claim, session=session
        )

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

    async def _notify_admission_or_settle(
        self, session: Any, *, task_id: str, run_id: str, context_id: str, item_id: str
    ) -> None:
        """Settle only notification failures, never rerun or undo admission."""
        from arbora.agent_runs import NativeRunAdmission, notify_native_admission

        try:
            notify_native_admission(
                session, NativeRunAdmission(task_id, run_id, context_id, item_id)
            )
        except (Exception, asyncio.CancelledError) as failure:
            try:
                async with asyncio.timeout(5):
                    cancelled = await self.cancel(task_id)
                    readback = await asyncio.to_thread(
                        self._authorized_item, task_id, "kg:write"
                    )
                    if (
                        cancelled.status.state != A2ATaskState.CANCELED
                        or not isinstance(readback, dict)
                        or readback.get("status") != "cancelled"
                    ):
                        raise RuntimeError("cancellation not confirmed")
            except (Exception, asyncio.CancelledError):
                logger.warning("notify admission or settle failed", exc_info=True)
                raise RuntimeError(
                    "native admission notification failed: settlement unavailable"
                ) from None
            if isinstance(failure, asyncio.CancelledError):
                raise
            raise RuntimeError(
                "native admission notification failed: admitted task canceled"
            ) from None

    #: Refusals the engine decides on a child admission; Arbora passes them through.
    _CHILD_REFUSALS = frozenset(
        {
            "CHILD_TARGET_TERMINAL",
            "CHILD_REVISION_STALE",
            "CHILD_OWNER_MISMATCH",
            "CHILD_LIMIT_EXHAUSTED",
            "CONTINUATION_HISTORY_UNSUPPORTED",
            "CONTINUATION_LIFECYCLE_INVALID",
            "HISTORY_REVISION_CONFLICT",
        }
    )

    def _admit(
        self, request: dict[str, Any], session: Any, owner_ref: str
    ) -> tuple[str, bool]:
        from arbora.api.invoke.pipeline import OperationRefused

        _engine, work_engine = self._engine()
        try:
            return self._submit_admission(work_engine, session, owner_ref, **request)
        except OperationRefused:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "")
            if code == "IDEMPOTENCY_CONFLICT":
                raise A2AIdempotencyConflict(
                    "idempotency key conflicts with admission"
                ) from exc
            if code in self._CHILD_REFUSALS:
                raise OperationRefused(code, {}) from None
            raise

    @staticmethod
    def _submit_admission(
        work_engine: Any, session: Any, owner_ref: str, **fields: Any
    ) -> tuple[str, bool]:
        from arbora import work_items

        return work_items.submit_work_item_atomic(
            work_engine,
            tenant=session.tenant,
            created_by=owner_ref,
            **fields,
        )

    async def admit_child_message(
        self,
        *,
        conversation_target: str,
        expected_revision: int,
        content: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Admit one message to an active child (ARBORA-RELEASE-R003.4.12).

        The identity binds caller, root owner, target, mode and the content digest;
        the same identity returns the same receipt and different content conflicts.
        The engine records the admission and the lifecycle revision, the harness
        delivers at its step boundary, and Arbora keeps no mailbox.
        """
        from arbora.api.invoke.pipeline import OperationRefused

        if len(content.encode("utf-8")) > CHILD_INPUT_MAX_BYTES:
            raise OperationRefused("CHILD_INPUT_TOO_LARGE", {})
        session = self._session("kg:write")
        owner_ref = self._owner_ref(session)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        key = idempotency_key.strip()
        if not key or len(key) > 512:
            raise ValueError("A2A idempotency key is invalid")
        item_id = "child-input:" + _digest(
            {"tenant": session.tenant, "owner": owner_ref, "key": key}
        )
        identity = {
            "mode": "message",
            "owner": owner_ref,
            "target": conversation_target,
            "content_digest": digest,
        }
        created_id, created = await asyncio.to_thread(
            self._admit,
            {
                "kind": "child_input",
                "work_item_id": item_id,
                "idempotency_key": key,
                "queue": "child_input",
                "payload_ref": conversation_target,
                "metadata": {**identity, "content": content},
                "child_input": {
                    "child_run_id": conversation_target,
                    "content_digest": digest,
                    "expected_revision": expected_revision,
                    "idempotency_key": key,
                    "state": "queued",
                },
            },
            session,
            owner_ref,
        )
        return {
            "work_item_id": created_id,
            "conversation_target": conversation_target,
            "content_digest": digest,
            "state": "queued",
            "created": created,
        }

    async def admit_continuation(
        self,
        *,
        conversation_target: str,
        history_revision: int,
        content: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Admit one run continuing a completed child (ARBORA-RELEASE-R003.4.13).

        The engine's ``ConversationTurnAdmission`` checks the owner, the completed
        lifecycle and the history revision; a refusal starts nothing and a terminal
        run is never reopened. Arbora keeps no history store.
        """
        from arbora import work_items
        from arbora.api.invoke.pipeline import OperationRefused

        session = self._session("kg:write")
        owner_ref = self._owner_ref(session)
        _engine, work_engine = self._engine()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        run_id = self._task_id(session.tenant, owner_ref, idempotency_key)
        predecessor = await asyncio.to_thread(
            work_items.get_work_item,
            work_engine,
            self._work_item_id(conversation_target),
        )
        # A foreign or absent predecessor answers alike: no run starts.
        if predecessor is None or not self._owned(predecessor, owner_ref):
            raise OperationRefused("CHILD_OWNER_MISMATCH", {})
        route = A2ARouteDecision.model_validate(predecessor["metadata"]["a2a_route"])
        context_id = str(predecessor["metadata"].get("a2a_context_id") or run_id)
        item_id, created = await asyncio.to_thread(
            self._admit,
            {
                "kind": "orchestrator_task",
                "run_id": run_id,
                "idempotency_key": run_id,
                "queue": "orchestrator_task",
                "resource_class": "agent_dispatch",
                "payload_ref": run_id,
                "description": content,
                "metadata": {
                    "a2a_schema": _A2A_METADATA_SCHEMA,
                    "a2a_owner_ref": owner_ref,
                    "a2a_context_id": context_id,
                    "a2a_route": route.model_dump(mode="json", by_alias=True),
                    "a2a_request_digest": _digest(
                        {
                            "mode": "continuation",
                            "target": conversation_target,
                            "history_revision": history_revision,
                            "content_digest": digest,
                        }
                    ),
                    "mode": "continuation",
                    "owner": owner_ref,
                    "target": conversation_target,
                    "content_digest": digest,
                    "a2a_run_id": run_id,
                },
                "conversation": {
                    "context_id": conversation_target,
                    "expected_revision": history_revision,
                    "predecessor_run_id": conversation_target,
                    "schema_version": "1",
                },
            },
            session,
            owner_ref,
        )
        if created:
            # One new run on the predecessor's chosen agent; a replay starts nothing.
            self._detach(
                self.runs._run(
                    work_engine,
                    item_id=item_id,
                    session=session,
                    agent_name=route.agent_name,
                    task=content,
                    task_id=run_id,
                    context_id=context_id,
                    run_id=run_id,
                ),
                item_id,
            )
        return {
            "work_item_id": item_id,
            "run_id": run_id,
            "predecessor_run_id": conversation_target,
            "agent_name": route.agent_name,
            "created": created,
        }

    async def _with_team(
        self,
        metadata: dict[str, Any],
        session: Any,
        message: A2AMessage,
        task_id: str,
        owner_ref: str,
        decision: A2ARouteDecision,
    ) -> dict[str, Any]:
        """The one team-coordination hook of ``dispatch`` (ARBORA-OPS-R877)."""
        if self.team is None:
            return metadata
        record = await self.team(
            session,
            message,
            task_id,
            owner_ref,
            decision.agent_name,
            int(self.clock() * 1000),
        )
        return {**metadata, "a2a_team": record}

    async def dispatch(
        self,
        *,
        message: A2AMessage,
        idempotency_key: str,
        decision: A2ARouteDecision,
        blocking: bool = True,
        model_profile: Any = None,
        media: dict[str, Any] | None = None,
        harness: Any = None,
    ) -> A2ATask:
        # Refuse before durable admission unless the harness carries and enforces
        # the subset at execution time (ARBORA-A2A-R002.1, R002.2.1).
        require_tool_subset_carrier(decision.selected_tools)
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
            model_profile=model_profile,
        )
        if harness is not None:
            metadata["a2a_harness"] = harness_metadata(harness)
        metadata = await self._with_team(
            metadata, session, message, task_id, owner_ref, decision
        )
        from arbora.access.task_guard import screen_and_redact_agent_task

        description = screen_and_redact_agent_task(message.task_text())
        metadata = await self._with_delegation(
            metadata,
            work_engine,
            session=session,
            task_id=task_id,
            agent_name=decision.agent_name,
        )
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
        await self._notify_admission_or_settle(
            session,
            task_id=task_id,
            run_id=run_id,
            context_id=context_id,
            item_id=item_id,
        )
        if not created and item.get("status") in _TERMINAL:
            recovered = await self.get(task_id)
            if recovered is None:
                raise RuntimeError("admitted A2A task became unavailable")
            return recovered
        usage = None
        if not blocking:
            # ARBORA-A2A-R001.6: answer with the admitted task now. The run
            # continues under that task; the caller follows it with
            # ``tasks/get``. The detached run inherits the caller's session
            # through the copied context.
            self._detach(
                self.runs._run(
                    work_engine,
                    item_id=item_id,
                    session=session,
                    agent_name=decision.agent_name,
                    task=description,
                    task_id=task_id,
                    context_id=context_id,
                    run_id=run_id,
                    media=media,
                ),
                item_id,
            )
        else:
            usage = await self.runs._run(
                work_engine,
                item_id=item_id,
                session=session,
                agent_name=decision.agent_name,
                task=description,
                task_id=task_id,
                context_id=context_id,
                run_id=run_id,
                media=media,
            )
            refreshed = await asyncio.to_thread(
                work_items.get_work_item, work_engine, item_id
            )
            if isinstance(refreshed, dict):
                item = refreshed
        return await self._project_dispatch_task(
            item, task_id=task_id, context_id=context_id, session=session, usage=usage
        )

    async def _project_dispatch_task(
        self,
        item: dict[str, Any],
        *,
        task_id: str,
        context_id: str,
        session: Any,
        usage: Any,
    ) -> A2ATask:
        """Keep native receipt attachment separate from dispatch control flow."""
        task = self.project_held(item, task_id=task_id)
        if self.task_plan_config is not None:
            await self._attach_run_usage(task, item, session)
        elif usage is not None:
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
        from medulla import EngineResponseError

        try:
            item = work_items.get_work_item(work_engine, self._work_item_id(task_id))
        except EngineResponseError as exc:
            if exc.code != "ACCESS_DENIED":
                raise
            return None
        return item if self._owned(item, self._owner_ref(session)) else None

    def project_held(self, item: dict[str, Any], *, task_id: str) -> A2ATask:
        """The task of ``item``, with the held reply of a succeeded run."""
        return self.projection.held_task(item, task_id=task_id)

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
        task = self.project_held(item, task_id=task_id)
        await self._attach_run_usage(task, item, read_session)
        try:
            task.artifacts, held = await asyncio.to_thread(
                self.projection._persisted_reply, task_id, item=item
            )
            task.metadata.update(held_metadata(held))
        except _ReplyUnavailable as exc:
            task.artifacts = None
            task.metadata["arbora"]["reply"] = exc.availability.model_dump(mode="json")
        if self._session("kg:read") != read_session:
            raise PermissionError("A2A caller changed during reply read")
        if include_committed_event:
            await self._attach_terminal_event(task, task_id, original)
        return task

    async def _attach_run_usage(
        self, task: A2ATask, item: dict[str, Any], session: Any
    ) -> None:
        """Project only caller-bound native completion, never trace counters."""
        from arbora import work_items

        from .run_usage import read_run_usage

        if not item.get("metadata", {}).get("a2a_run_id"):
            return
        if self._session("kg:read") != session:
            raise PermissionError("A2A caller changed during usage read")
        engine, work_engine = self._engine()
        usage = await read_run_usage(
            engine,
            session=session,
            task=task,
            item=item,
            owner_ref=self._owner_ref(session),
            owner_graph=work_items.native_owner_graph(work_engine),
        )
        if self._session("kg:read") != session:
            raise PermissionError("A2A caller changed during usage read")
        task.metadata["arbora"]["usage"] = usage

    async def _attach_terminal_event(
        self, task: A2ATask, task_id: str, session: Any
    ) -> None:
        """Read and attach a terminal event under the unchanged caller authority."""
        from arbora import work_items

        from .terminal_event import (
            A2ATerminalEventUnavailable,
            read_terminal_event,
        )

        owner_ref = self._owner_ref(session)
        self._validate_terminal_caller(session, owner_ref)
        engine, work_engine = self._engine()
        try:
            owner_graph = work_items.native_owner_graph(work_engine)
            event = await asyncio.wait_for(
                read_terminal_event(
                    engine,
                    session=session,
                    task_id=task_id,
                    owner_ref=owner_ref,
                    owner_graph=owner_graph,
                ),
                timeout=5.0,
            )
        except (TimeoutError, NativeWorkItemRequired) as exc:
            raise A2ATerminalEventUnavailable(
                "committed terminal event is unavailable"
            ) from exc
        self._validate_terminal_caller(session, owner_ref)
        if event["status"] == "available" and (
            _STATE_MAP[event["outcome"]] != task.status.state
        ):
            raise A2ATerminalEventUnavailable("committed terminal event is unavailable")
        task.metadata["arbora"]["terminalEvent"] = event

    def _validate_terminal_caller(self, session: Any, owner_ref: str) -> None:
        """Refuse changed caller authority on either side of a terminal read."""
        current = self._session("work:read")
        if (
            current != session
            or current.tenant != session.tenant
            or current.graph != session.graph
            or self._owner_ref(current) != owner_ref
        ):
            raise PermissionError("A2A caller changed during terminal read")

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

    def _project_list_row(self, row: Any, *, owner_ref: str) -> A2ATask:
        if not self._owned(row, owner_ref):
            raise RuntimeError("canonical WorkItem list crossed its authority scope")
        item_id = str(row.get("work_item_id") or "")
        task_id = (
            item_id
            if row["metadata"].get("a2a_run_id")
            else item_id.removeprefix(_WORK_ITEM_PREFIX)
        )
        if self._work_item_id(task_id) != item_id:
            raise RuntimeError("canonical WorkItem list returned an invalid identity")
        return self.project_held(row, task_id=task_id)

    async def list(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[A2ATask], str | None]:
        if not 1 <= limit <= 100:
            raise ValueError("A2A task page limit must be between 1 and 100")
        session = self._session("kg:read")
        owner_ref = self._owner_ref(session)
        native_cursor = self.projection._decode_cursor(
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
        rows, continuation = self.projection._validated_list_page(
            page, limit, native_cursor
        )
        tasks = [self._project_list_row(row, owner_ref=owner_ref) for row in rows]
        next_cursor = (
            self.projection._cursor(owner_ref, session.tenant, continuation)
            if continuation is not None
            else None
        )
        return tasks, next_cursor

    async def cancel(self, task_id: str) -> A2ATask:
        cancellation_session = self._session("kg:write")
        item = await asyncio.to_thread(self._authorized_item, task_id, "kg:write")
        if item is None:
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        run_id = str(item["metadata"].get("a2a_run_id") or task_id)
        if item.get("status") == "cancelled":
            self._origins.revoke(cancellation_session, run_id)
            return self.project_held(item, task_id=task_id)
        if item.get("status") in _TERMINAL:
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        from arbora import work_items

        _engine, work_engine = self._engine()
        item_id = self._work_item_id(task_id)
        if not await asyncio.to_thread(
            work_items.cancel_work_item, work_engine, item_id
        ):
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        self._origins.revoke(cancellation_session, run_id)
        await self.runs._publish(
            item_id,
            None,
            task_id=task_id,
            context_id=str(item["metadata"].get("a2a_context_id") or ""),
            kind="status-update",
            state="canceled",
            final=True,
        )
        current = await asyncio.to_thread(
            work_items.get_work_item, work_engine, item_id
        )
        if not isinstance(current, dict):
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        return self.project_held(current, task_id=task_id)
