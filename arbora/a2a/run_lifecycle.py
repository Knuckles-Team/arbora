"""Running a claimed WorkItem: the agent turn, its reply, the outcome commit and the published events (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable as _Callable
from contextlib import ExitStack
from typing import Any, NamedTuple

from scion_sdk.privacy import persistence_reference

logger = logging.getLogger(__name__)


_WORK_ITEM_PREFIX = "workitem:orchestrator:"


#: How long one agent run may hold its WorkItem lease.
_RUN_LEASE_SECONDS = 600.0


_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})


_MAX_OUTPUT_CHARS = 65_536


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


class _Attempt(NamedTuple):
    """What one agent attempt produced: its outcome, refs, usage and reply."""

    outcome: str
    result_ref: str | None
    error_ref: str | None
    usage: Any
    reply: str | None


class RunLifecycle:
    """Runs one claimed WorkItem and publishes what it produced.

    Built from the shared state and the calls it needs, with no reference to
    the authority that owns that state."""

    def __init__(
        self,
        *,
        engine: _Callable[..., Any],
        executor: _Callable[[], Any],
        held_output_refs: Any,
        clock: _Callable[[], Any],
        delegation_reader: _Callable[[], Any],
        events: _Callable[[], Any],
        held_output_graph: _Callable[[], Any],
        held_output_source: _Callable[[], Any],
        hosted_invocation: _Callable[[], Any],
        outputs: Any,
        push: _Callable[[], Any],
        task_plan_config: _Callable[[], Any],
    ) -> None:
        self._engine = engine
        self._get_executor = executor
        self._held_output_refs = held_output_refs
        self._get_clock = clock
        self._get_delegation_reader = delegation_reader
        self._get_events = events
        self._get_held_output_graph = held_output_graph
        self._get_held_output_source = held_output_source
        self._get_hosted_invocation = hosted_invocation
        self.outputs = outputs
        self._get_push = push
        self._get_task_plan_config = task_plan_config

    def _agent_executor(self) -> Any:
        if self._get_executor() is None:
            raise RuntimeError("the agent run port is not composed")
        return self._get_executor()

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
        from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
        from arbora.core.agent_names import preloaded_agent_tools

        if (
            self._get_task_plan_config() is not None
            and self._get_hosted_invocation() is None
        ):
            raise RuntimeError("UPGRADE_REQUIRED: hosted task-plan invocation")
        run_session, acting_scope = _acting_scope(session, acting_session)
        subset = await self._selected_tools(work_engine, item_id)
        with ExitStack() as scopes:
            self._enter_scopes(
                scopes,
                acting_scope,
                work_engine,
                item_id=item_id,
                session=session,
                run_session=run_session,
                claim=claim,
                task_id=task_id,
                context_id=context_id,
                run_id=run_id,
                observer=observer,
                subset=subset,
            )
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

    def _enter_scopes(
        self,
        scopes: ExitStack,
        acting_scope: Any,
        work_engine: Any,
        *,
        item_id: str,
        session: Any,
        run_session: Any,
        claim: Any,
        task_id: str,
        context_id: str,
        run_id: str | None,
        observer: Any,
        subset: Any,
    ) -> None:
        """Enter the owner, claim and stream scopes one run executes inside."""
        from arbora import work_items
        from arbora.agent_runs import native_run_scope, native_stream_scope
        from arbora.current_plan_publication import bind_admitted_claim

        native = self._get_task_plan_config() is not None
        scopes.enter_context(acting_scope)
        scopes.enter_context(
            native_run_scope(
                run_id=run_id or task_id,
                task_id=task_id,
                context_id=context_id,
                work_item_id=item_id,
                session=run_session,
                claim=claim,
                owner_graph=work_items.native_owner_graph(work_engine)
                if native
                else None,
                hosted_invocation=self._get_hosted_invocation(),
                held_output=self._held_refs(task_id, session),
                tool_subset=subset,
            )
        )
        if native:
            scopes.enter_context(
                bind_admitted_claim(
                    work_engine,
                    session=run_session,
                    run_id=run_id or task_id,
                    work_item_id=item_id,
                    claim=claim,
                    hosted_invocation=self._get_hosted_invocation(),
                    owner_graph=work_items.native_owner_graph(work_engine),
                )
            )
        if observer is not None:
            scopes.enter_context(native_stream_scope(run_session, observer))

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
            now=self._get_clock()(),
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
            read=self._get_delegation_reader(),
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
        attempt = await self._attempt(
            work_engine,
            claim,
            observer,
            item_id=item_id,
            session=session,
            agent_name=agent_name,
            task=task,
            task_id=task_id,
            context_id=context_id,
            run_id=run_id,
            media=media,
            delegated_operations=delegated_operations,
            acting_session=acting_session,
        )
        reply = attempt.reply
        if reply is not None:
            reply = reply[:_MAX_OUTPUT_CHARS]
        answered = await self._finish_reply(
            observer, session, task_id, reply, attempt.outcome
        )
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
            state="completed" if attempt.outcome == "succeeded" else "failed",
            final=True,
        )
        await self._commit_outcome(
            work_engine,
            item_id,
            claim,
            outcome=attempt.outcome,
            result_ref=attempt.result_ref,
            error_ref=attempt.error_ref,
            task_id=task_id,
            context_id=context_id,
            completion_kwargs=self._completion_kwargs(attempt.usage, run_id or task_id),
        )
        return attempt.usage

    def _completion_kwargs(self, usage: Any, run_id: str) -> dict[str, Any]:
        """The run completion receipt a native task plan commits with its result."""
        if self._get_task_plan_config() is None:
            return {}
        return {
            "run_completion": {
                "schema_version": 1,
                "run_id": run_id,
                **({"usage": usage} if usage is not None else {}),
            }
        }

    async def _attempt(
        self,
        work_engine: Any,
        claim: dict[str, Any],
        observer: Any,
        *,
        item_id: str,
        session: Any,
        agent_name: str,
        task: str,
        task_id: str,
        context_id: str,
        run_id: str | None,
        media: dict[str, Any] | None,
        delegated_operations: tuple[str, ...] | None,
        acting_session: Any,
    ) -> _Attempt:
        """Run the agent once; a failed run is a failed attempt, never an exception."""
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
            output = getattr(result, "output", None)
            self.outputs.remember(task_id, output)
            return _Attempt(
                "succeeded",
                persistence_reference("run", result.run_id, namespace="arbora-a2a"),
                None,
                getattr(result, "usage", None),
                output if isinstance(output, str) and output else None,
            )
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "a failed run is a failed task", exc_info=True
            )
            logging.getLogger(__name__).warning(
                "A2A run %s failed: %s: %s", item_id, type(exc).__name__, str(exc)[:300]
            )
            from arbora.agent_runs import failed_run_usage

            self.outputs.forget(task_id)
            return _Attempt(
                "failed",
                None,
                persistence_reference(
                    "error", type(exc).__name__, namespace="arbora-a2a"
                ),
                failed_run_usage(exc, run_id or task_id),
                None,
            )

    async def _finish_reply(
        self,
        observer: Any,
        session: Any,
        task_id: str,
        reply: str | None,
        outcome: str,
    ) -> bool:
        """Hold the reply as an output (R022.1), then finish its stream; was it answered."""
        if (
            reply
            and outcome == "succeeded"
            and self._get_held_output_source() is not None
        ):
            await self._get_held_output_source().produce(
                self._engine()[0],
                tenant=str(session.tenant),
                task_id=task_id,
                text=reply,
                graph=self._get_held_output_graph(),
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
            if self._get_events() is not None:
                self._get_events().notify(item_id)
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
        if self._get_events() is not None:
            self._get_events().notify(item_id)

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
        if self._get_events() is not None:
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
        if final and self._get_push() is not None:
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
                await asyncio.to_thread(self._get_push().enqueue, task_id, item)
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
        await self._get_events().publish(item_id, draft, claim=claim)

    def _run_observer(
        self, item_id: str, claim: Any, *, task_id: str, context_id: str
    ) -> Any:
        """The run's stream observer, publishing through the one event log.

        ``None`` when no event log is wired or an enclosing host already
        observes the run.
        """
        from arbora.agent_runs import native_stream_open

        if self._get_events() is None or native_stream_open():
            return None
        from arbora.api.invoke.screening import screen_settings

        from .run_events import RunEventObserver

        async def publish(draft: dict[str, Any]) -> None:
            await self._get_events().publish(item_id, draft, claim=claim)

        return RunEventObserver(
            publish,
            task_id=task_id,
            context_id=context_id,
            screen=screen_settings(self._get_events().config),
        )
