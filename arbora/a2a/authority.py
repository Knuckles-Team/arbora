"""Canonical WorkItem/RunTrace authority used by every unary A2A surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

from scion_sdk.privacy import persistence_reference

from arbora.core.work_item_errors import NativeWorkItemRequired
from arbora.run_origin import RunOriginRegistry

from .held_output import held_metadata
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
from .tool_subset import require_carrier as require_tool_subset_carrier

logger = logging.getLogger(__name__)


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
#: The most content one child message carries (ARBORA-RELEASE-R003.4.12).
CHILD_INPUT_MAX_BYTES = 16_384
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


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _delegated_tools(preloaded: Any, operations: tuple[str, ...] | None) -> Any:
    """The run's tools bounded by its delegation: ``None`` operations leave them as is."""
    if operations is None:
        return preloaded
    held = frozenset(operations)
    if preloaded is None:
        return tuple(op for op in operations if not op.startswith("agent:"))
    return tuple(tool for tool in preloaded if tool in held)


def _acting_scope(session: Any, acting_session: Any) -> tuple[Any, Any]:
    """The session a run executes under and the scope that binds it.

    A reclaimed run carries the host session with its delegation; any other run keeps
    the session it was admitted under and binds nothing new.
    """
    from contextlib import nullcontext

    from arbora.security.session import use_session

    if acting_session is None:
        return session, nullcontext()
    return acting_session, use_session(acting_session)


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
    ) -> None:
        self._engine_provider = engine_provider
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

    async def _publish(
        self,
        item_id: str,
        claim: Any,
        *,
        task_id: str,
        context_id: str,
        kind: str,
        state: str | None = None,
        final: bool = False,
        reply: str | None = None,
    ) -> None:
        """Write one A2A-visible run event under the run's lease fence."""
        if self.events is not None:
            await self._publish_event(
                item_id,
                claim,
                task_id=task_id,
                context_id=context_id,
                kind=kind,
                state=state,
                final=final,
                reply=reply,
            )
        if final and self.push is not None:
            await self._push_terminal(item_id, task_id)

    async def _push_terminal(self, item_id: str, task_id: str) -> None:
        """Submit one push item per registered target of a terminal task (R013.2).

        The task is already terminal, so a failure here never fails the run.
        """
        from arbora import work_items

        try:
            item = await asyncio.to_thread(
                work_items.get_work_item, self._engine()[1], item_id
            )
            if isinstance(item, dict):
                await asyncio.to_thread(self.push.enqueue, task_id, item)
        except Exception:  # terminal state is already committed
            logging.getLogger(__name__).exception("a2a push enqueue failed")

    async def _publish_event(
        self,
        item_id: str,
        claim: Any,
        *,
        task_id: str,
        context_id: str,
        kind: str,
        state: str | None,
        final: bool,
        reply: str | None,
    ) -> None:
        draft: dict[str, Any] = {
            "task_id": task_id,
            "context_id": context_id,
            "kind": kind,
            "state": state,
            "final": final,
        }
        if reply is not None:
            draft["artifact"] = {
                "artifactId": f"{task_id}:reply",
                "name": "reply",
                "parts": [{"kind": "text", "text": reply}],
            }
        await self.events.publish(item_id, draft, claim=claim)

    def _run_observer(
        self, item_id: str, claim: Any, *, task_id: str, context_id: str
    ) -> Any:
        """The run's stream observer, publishing through the one event log.

        ``None`` when no event log is wired or an enclosing host already
        observes the run.
        """
        from arbora.agent_runs import native_stream_open

        if self.events is None or native_stream_open():
            return None
        from .run_events import RunEventObserver

        async def publish(draft: dict[str, Any]) -> None:
            await self.events.publish(item_id, draft, claim=claim)

        return RunEventObserver(publish, task_id=task_id, context_id=context_id)

    async def owned_item(self, task_id: str) -> dict[str, Any] | None:
        """The caller's own task record, or ``None`` for an absent or foreign task."""
        try:
            return await asyncio.to_thread(self._authorized_item, task_id, "kg:read")
        except ValueError:
            return None

    def _agent_executor(self) -> Any:
        if self._executor is None:
            from arbora.agent_runs import AgentRunPort

            self._executor = AgentRunPort(origins=self._origins, ports=self._ports)
        return self._executor

    def _held_refs(self, task_id: str, session: Any) -> Any:
        """The received refs of this run's held output, or ``None`` (none composed)."""
        provider = self._held_output_refs
        return None if provider is None else provider(task_id, session)

    async def _selected_tools(self, work_engine: Any, item_id: str) -> tuple[str, ...]:
        """The tool subset the routing decision selected, read from the durable item."""
        from arbora import work_items

        from .tool_subset import carrier_available, selected_tools_of

        if not carrier_available():
            return ()  # PIN-LAG: dispatch refused any selected subset at admission
        item = await asyncio.to_thread(work_items.get_work_item, work_engine, item_id)
        return selected_tools_of(item or {})

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
        media: dict[str, Any] | None = None,
        observer: Any = None,
        delegated_operations: tuple[str, ...] | None = None,
        acting_session: Any = None,
    ) -> Any:
        """Execute only inside the host's revocable native owner scope.

        A reclaimed run passes ``acting_session``: the host session carrying the run's
        delegation, so every engine call of the run is made as the run's owner.
        """
        from arbora import work_items
        from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest

        if self.task_plan_config is not None and self.hosted_invocation is None:
            raise RuntimeError("UPGRADE_REQUIRED: hosted task-plan invocation")
        from contextlib import nullcontext

        from arbora.agent_runs import native_run_scope, native_stream_scope
        from arbora.core.agent_names import preloaded_agent_tools
        from arbora.current_plan_publication import bind_admitted_claim

        run_session, acting_scope = _acting_scope(session, acting_session)
        subset = await self._selected_tools(work_engine, item_id)
        with (
            acting_scope,
            native_run_scope(
                run_id=run_id or task_id,
                task_id=task_id,
                context_id=context_id,
                work_item_id=item_id,
                session=run_session,
                claim=claim,
                owner_graph=(
                    work_items.native_owner_graph(work_engine)
                    if self.task_plan_config is not None
                    else None
                ),
                hosted_invocation=self.hosted_invocation,
                held_output=self._held_refs(task_id, session),
                tool_subset=subset,
            ),
            (
                bind_admitted_claim(
                    work_engine,
                    session=run_session,
                    run_id=run_id or task_id,
                    work_item_id=item_id,
                    claim=claim,
                    hosted_invocation=self.hosted_invocation,
                    owner_graph=work_items.native_owner_graph(work_engine),
                )
                if self.task_plan_config is not None
                else nullcontext()
            ),
            (
                native_stream_scope(run_session, observer)
                if observer is not None
                else nullcontext()
            ),
        ):
            return await self._agent_executor().execute_agent(
                AgentExecutionRequest(
                    agent_name=agent_name,
                    task=task,
                    run_id=run_id or task_id or None,
                    allowed_tools=_delegated_tools(
                        preloaded_agent_tools(agent_name), delegated_operations
                    ),
                    **(media or {}),
                ),
                session=run_session,
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
        media: dict[str, Any] | None = None,
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
            now=self.clock(),
        )
        if claim is None:
            return None
        return await self._run_claimed(
            work_engine,
            claim,
            item_id=item_id,
            session=session,
            agent_name=agent_name,
            task=task,
            task_id=task_id,
            context_id=context_id,
            run_id=run_id,
            media=media,
        )

    async def resume_reclaimed(
        self,
        work_engine: Any,
        item: dict[str, Any],
        claim: dict[str, Any],
        *,
        session: Any,
    ) -> dict[str, int] | None:
        """Resume a run whose lease expired, under the claim the consumer won.

        The run restarts under the new lease fence. The harness restores the
        run from ``EngineHostCheckpoint`` (``MERISTEM-BOUNDARY-R043.8``), and the
        event log continues from the committed ``event_seq``: the ``working``
        event of the first attempt is not published again.

        The run acts under its own ``agent_run`` delegation (``delegation_id`` of the
        claim), not the host's process session: the delegation's principal must be
        the run's owner, and the run's tools are bounded by the delegation's
        operations. A foreign, absent, revoked or expired delegation raises
        ``RunDelegationRefused`` before any step runs.
        """
        from .run_delegation import acting_session, resolve_run_delegation

        delegation = await resolve_run_delegation(
            item,
            claim,
            read=self.delegation_reader,
            tenant=str(session.tenant),
        )
        acting = acting_session(session, delegation)
        metadata = item["metadata"]
        item_id = str(item["work_item_id"])
        route = metadata.get("a2a_route") or {}
        agent_name = str(route.get("agent_name") or route.get("agentName") or "")
        task_id = item_id.removeprefix(_WORK_ITEM_PREFIX)
        return await self._run_claimed(
            work_engine,
            claim,
            item_id=item_id,
            session=session,
            agent_name=agent_name,
            task=str(metadata.get("description") or ""),
            task_id=task_id,
            context_id=str(metadata.get("a2a_context_id") or ""),
            run_id=str(metadata.get("a2a_run_id") or task_id),
            resumed=True,
            delegated_operations=delegation.operations,
            acting_session=acting,
        )

    async def _run_claimed(
        self,
        work_engine: Any,
        claim: dict[str, Any],
        *,
        item_id: str,
        session: Any,
        agent_name: str,
        task: str,
        task_id: str,
        context_id: str,
        run_id: str | None,
        media: dict[str, Any] | None = None,
        resumed: bool = False,
        delegated_operations: tuple[str, ...] | None = None,
        acting_session: Any = None,
    ) -> dict[str, int] | None:
        """Run the agent under a lease already held and commit its outcome."""
        outcome, result_ref, error_ref = "succeeded", None, None
        usage = None
        reply: str | None = None
        if not resumed:
            await self._publish(
                item_id,
                claim,
                task_id=task_id,
                context_id=context_id,
                kind="status-update",
                state="working",
            )
        observer = self._run_observer(
            item_id, claim, task_id=task_id, context_id=context_id
        )
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
                media=media,
                observer=observer,
                delegated_operations=delegated_operations,
                acting_session=acting_session,
            )
            if result.run_id != (run_id or task_id):
                raise RuntimeError("agent result does not belong to this A2A turn")
            result_ref = persistence_reference(
                "run", result.run_id, namespace="arbora-a2a"
            )
            usage = getattr(result, "usage", None)
            remember_task_output(task_id, getattr(result, "output", None))
            output = getattr(result, "output", None)
            reply = output if isinstance(output, str) and output else None
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "a failed run is a failed task", exc_info=True
            )
            logging.getLogger(__name__).warning(
                "A2A run %s failed: %s: %s", item_id, type(exc).__name__, str(exc)[:300]
            )
            from arbora.agent_runs import failed_run_usage

            usage = failed_run_usage(exc, run_id or task_id)
            outcome = "failed"
            result_ref = None
            _OUTPUTS.pop(task_id, None)
            error_ref = persistence_reference(
                "error", type(exc).__name__, namespace="arbora-a2a"
            )
        if reply is not None:
            reply = reply[:_MAX_OUTPUT_CHARS]
        answered = await self._finish_reply(observer, session, task_id, reply, outcome)
        if reply is not None and not answered:
            await self._publish(
                item_id,
                claim,
                task_id=task_id,
                context_id=context_id,
                kind="artifact-update",
                reply=reply,
            )
        # The final event is durable before the result commits (the lease fence
        # needs that order). The stream holds it back until the commit lands.
        await self._publish(
            item_id,
            claim,
            task_id=task_id,
            context_id=context_id,
            kind="status-update",
            state="completed" if outcome == "succeeded" else "failed",
            final=True,
        )
        completion_kwargs: dict[str, Any] = {}
        if self.task_plan_config is not None:
            completion_kwargs["run_completion"] = {
                "schema_version": 1,
                "run_id": run_id or task_id,
                **({"usage": usage} if usage is not None else {}),
            }
        await self._commit_outcome(
            work_engine,
            item_id,
            claim,
            outcome=outcome,
            result_ref=result_ref,
            error_ref=error_ref,
            task_id=task_id,
            context_id=context_id,
            completion_kwargs=completion_kwargs,
        )
        return usage

    async def _finish_reply(
        self,
        observer: Any,
        session: Any,
        task_id: str,
        reply: str | None,
        outcome: str,
    ) -> bool:
        """Hold the reply as an output (R022.1), then finish its stream; was it answered."""
        if reply and outcome == "succeeded" and self.held_output_source is not None:
            await self.held_output_source.produce(
                self._engine()[0],
                tenant=str(session.tenant),
                task_id=task_id,
                text=reply,
                graph=self.held_output_graph,
            )
        return await observer.finish(reply) if observer is not None else False

    async def _commit_outcome(
        self,
        work_engine: Any,
        item_id: str,
        claim: Any,
        *,
        outcome: str,
        result_ref: str | None,
        error_ref: str | None,
        task_id: str,
        context_id: str,
        completion_kwargs: dict[str, Any],
    ) -> None:
        """Commit the outcome, then wake the streams that wait on it.

        A commit that does not apply moves a ``succeeded`` run to ``failed``
        through the same edge: a final ``failed`` event, then the ``failed``
        commit. The stream delivers the committed state and never the
        ``completed`` event that was written ahead of the commit.
        """
        from arbora import work_items

        try:
            status = await asyncio.to_thread(
                work_items.commit_result,
                work_engine,
                item_id,
                claim,
                outcome=outcome,
                result_ref=result_ref,
                error_ref=error_ref,
                **completion_kwargs,
            )
        except Exception as exc:  # the failed edge is the answer
            if outcome != "succeeded":
                raise
            logging.getLogger(__name__).warning(
                "A2A run %s commit failed: %s", item_id, type(exc).__name__
            )
            status = "commit_failed"
        applied = not isinstance(status, str) or status in {"committed", "noop"}
        if applied or status in _TERMINAL or outcome != "succeeded":
            if self.events is not None:
                self.events.notify(item_id)
            return
        await self._publish(
            item_id,
            claim,
            task_id=task_id,
            context_id=context_id,
            kind="status-update",
            state="failed",
            final=True,
        )
        await asyncio.to_thread(
            work_items.commit_result,
            work_engine,
            item_id,
            claim,
            outcome="failed",
            error_ref=persistence_reference(
                "error", "CommitFailed", namespace="arbora-a2a"
            ),
            **completion_kwargs,
        )
        if self.events is not None:
            self.events.notify(item_id)

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
                        cancelled.status.state != "canceled"
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
                self._run(
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
                self._run(
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
            usage = await self._run(
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
        task = self._project(item, task_id=task_id)
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
        from .event_log import terminal_state

        state = (
            "failed"
            if terminal_state(item) == "failed" and raw_state == "cancelled"
            else _STATE_MAP.get(raw_state)
        )
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
            artifacts=task_output_artifacts(task_id)
            if raw_state == "succeeded"
            else None,
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

        held = held_block(event.get("result"), engine, task_id, self.held_output_source)
        return [reply_artifact(task_id, text)], held

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
        await self._attach_run_usage(task, item, read_session)
        try:
            task.artifacts, held = await asyncio.to_thread(
                self._persisted_reply, task_id, item=item
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
        cancellation_session = self._session("kg:write")
        item = await asyncio.to_thread(self._authorized_item, task_id, "kg:write")
        if item is None:
            raise A2ATaskNotCancelable("A2A task is not cancelable")
        run_id = str(item["metadata"].get("a2a_run_id") or task_id)
        if item.get("status") == "cancelled":
            self._origins.revoke(cancellation_session, run_id)
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
        self._origins.revoke(cancellation_session, run_id)
        await self._publish(
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
        return self._project(current, task_id=task_id)
