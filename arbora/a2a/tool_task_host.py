"""Host composition of the tool-task consumer (ARBORA-A2A-R002.2.3.2).

The host starts one :class:`~arbora.a2a.tool_task_consumer.ToolTaskConsumer` per
replica from three ports, none of which keeps state that correctness depends on:

* the engine WorkItem queue of kind ``tool_task``, read as verified carriers;
* the original-caller delegation read (``ARBORA-A2A-R002.2.3.3``);
* a dispatcher that runs the admitted operation through the one invoke pipeline
  as the host's service caller (``ServiceCallers.invoke``), keyed by the job id.

A host that has no engine client starts no consumer and reports ``UNAVAILABLE``
on ``/ready``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from arbora import work_items
from arbora.api.invoke.steps import OpError

from .tool_task import (
    TERMINAL_WORK_ITEM_STATES,
    ToolTaskCarrier,
    ToolTaskRefused,
    ToolTaskRequest,
    ToolTaskResult,
    tool_task_work_item_id,
)
from .tool_task_consumer import UNAVAILABLE, ToolTaskConsumer
from .tool_task_dispatch import EgWorkItemFence
from .tool_task_requests import EgToolTaskRequestStore

__all__ = [
    "HEALTH_PROBE",
    "LazyEngine",
    "EngineToolTaskQueue",
    "InvokeToolTaskDispatcher",
    "ToolTaskService",
    "build_tool_task_service",
    "unavailable_probe",
]

#: The readiness check the host binds for the consumer.
HEALTH_PROBE = "tool_task_consumer"
_KIND = "tool_task"
_PREFIX = "workitem:dispatch:"
_PAGE = 100
_READ_TIMEOUT_S = 30.0
_log = logging.getLogger("arbora.a2a.tool_task")


class LazyEngine:
    """The served engine, resolved on each use: the host may bind it after composition."""

    def __init__(self, provider: Callable[[], Any]) -> None:
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


class EngineToolTaskQueue:
    """The engine WorkItem queue ``tool_task``, listed under the ambient session.

    A carrier is built only from the engine's own record. The engine terminal
    state is the acknowledgement, so :meth:`acknowledge` writes nothing.
    """

    def __init__(self, engine: Any, *, tenant: str) -> None:
        self._engine = engine
        self._tenant = tenant

    def ready(self) -> list[ToolTaskCarrier]:
        carriers: list[ToolTaskCarrier] = []
        cursor: str | None = None
        while True:
            page = work_items.list_work_items(
                self._engine, kind=_KIND, cursor=cursor, limit=_PAGE
            )
            for item in page.get("items", ()):
                carrier = self._carrier(item)
                if carrier is not None:
                    carriers.append(carrier)
            cursor = page.get("next_cursor")
            if not cursor:
                return carriers

    def _carrier(self, item: Mapping[str, Any]) -> ToolTaskCarrier | None:
        item_id = item.get("work_item_id")
        status = item.get("status")
        metadata = item.get("metadata")
        if (
            not isinstance(item_id, str)
            or not item_id.startswith(_PREFIX)
            or status in TERMINAL_WORK_ITEM_STATES
            or not isinstance(metadata, Mapping)
            or not (status == "ready" or item.get("effect_phase") == "started")
        ):
            return None
        job_id = item_id[len(_PREFIX) :]
        try:
            carrier = ToolTaskCarrier(
                job_id=job_id,
                payload_ref=str(item.get("input_ref")),
                tenant=self._tenant,
                session_id=str(metadata.get("request_session")),
                allowed_tool_subset=(str(metadata.get("request_operation")),),
            )
        except ValueError:
            return None
        return carrier if tool_task_work_item_id(job_id) == item_id else None

    def acknowledge(self, carrier: ToolTaskCarrier) -> None:
        del carrier


class InvokeToolTaskDispatcher:
    """Run one admitted tool call through the one invoke pipeline.

    ``invoke`` is ``ServiceCallers.invoke``: the host's own service caller for
    the tenant, keyed by the job id so the pipeline never repeats an effect. The
    pipeline runs on the host loop; this dispatcher is called from the polling
    thread and waits for it.
    """

    def __init__(
        self,
        store: EgToolTaskRequestStore,
        invoke: Callable[[str, str, Mapping[str, Any], str], Any],
        loop: Callable[[], asyncio.AbstractEventLoop | None],
        *,
        timeout_s: float = 300.0,
    ) -> None:
        self._store = store
        self._invoke = invoke
        self._loop = loop
        self._timeout = timeout_s

    def resolve(
        self, request_ref: str, *, job_id: str, tenant: str, session_id: str
    ) -> ToolTaskRequest:
        return self._store.resolve(
            request_ref,
            job_id=job_id,
            tenant=tenant,
            session_id=session_id,
            service=True,
        )

    def execute(
        self,
        request: ToolTaskRequest,
        *,
        job_id: str,
        require_current: Callable[[], None],
    ) -> ToolTaskResult:
        require_current()
        loop = self._loop()
        if loop is None:
            raise ToolTaskRefused("tool_task_host_loop_unavailable")
        params = json.loads(request.parameters_json)
        outcome = asyncio.run_coroutine_threadsafe(
            self._invoke(request.tenant, request.operation_id, params, job_id), loop
        ).result(self._timeout)
        if isinstance(outcome, OpError):
            if outcome.source != "arbora":
                # A fleet or engine failure may follow an effect: uncertain.
                raise RuntimeError(f"tool task failed after dispatch: {outcome.code}")
            return self._result(request, job_id, "refused", f"refused:{outcome.code}")
        receipt = self._store.store_result(job_id, getattr(outcome, "value", outcome))
        return self._result(request, job_id, "completed", receipt)

    @staticmethod
    def _result(
        request: ToolTaskRequest, job_id: str, outcome: Any, receipt: str
    ) -> ToolTaskResult:
        ref = receipt if outcome == "completed" else f"tool-task:{job_id}:{receipt}"
        return ToolTaskResult(
            job_id=job_id,
            request_ref=request.request_ref,
            principal=request.principal,
            outcome=outcome,
            receipt_ref=ref,
        )

    def lookup(self, job_id: str, *, request: ToolTaskRequest) -> ToolTaskResult | None:
        """No connector serves an outcome read by job id yet: the item stays open."""
        del job_id, request
        return None


class ToolTaskService:
    """The host background service: one consumer task per replica."""

    def __init__(
        self,
        consumer: ToolTaskConsumer,
        *,
        loop_holder: list[asyncio.AbstractEventLoop],
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.consumer = consumer
        self._loops = loop_holder
        self._sleep = sleep
        self.running = False

    async def run(self, should_stop: Callable[[], bool]) -> None:
        self._loops[:] = [asyncio.get_running_loop()]
        self.consumer.start()
        self.running = True
        try:
            while not should_stop():
                await self._sleep(0.5)
        finally:
            self.running = False
            await self.consumer.stop()
            self._loops.clear()

    def probe(self, session: Any) -> dict[str, Any]:
        """The ``/ready`` check: polling, or ``UNAVAILABLE``."""
        del session
        if self.running:
            return {"status": "ok", "detail": {"state": "polling"}}
        return {"status": "unhealthy", "reason": UNAVAILABLE}


def unavailable_probe(session: Any) -> dict[str, Any]:
    """A host without the engine client reports ``UNAVAILABLE`` on ``/ready``."""
    del session
    return {"status": "unhealthy", "reason": UNAVAILABLE}


def build_tool_task_service(
    *,
    engine: Any,
    tenant: str,
    process_session: Callable[[], Any],
    invoke: Callable[[str, str, Mapping[str, Any], str], Any],
    read_delegation: Callable[[str], Any],
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Any] = asyncio.sleep,
    poll_interval_s: float = 2.0,
) -> ToolTaskService:
    """Compose the consumer from the engine queue, delegation read and dispatcher.

    ``read_delegation`` is the engine's async delegation reader; the polling
    thread waits for it on the host loop.
    """
    from arbora.security.session import use_session

    loops: list[asyncio.AbstractEventLoop] = []

    def host_loop() -> asyncio.AbstractEventLoop | None:
        return loops[0] if loops else None

    def read(delegation_id: str) -> Mapping[str, Any] | None:
        loop = host_loop()
        if loop is None:
            raise RuntimeError("host loop unavailable")
        return asyncio.run_coroutine_threadsafe(
            read_delegation(delegation_id), loop
        ).result(_READ_TIMEOUT_S)

    consumer = ToolTaskConsumer(
        queue=EngineToolTaskQueue(engine, tenant=tenant),
        fence=EgWorkItemFence(engine, tenant=tenant),
        dispatcher=InvokeToolTaskDispatcher(
            EgToolTaskRequestStore(engine), invoke, host_loop
        ),
        read_delegation=read,
        clock=clock,
        sleep=sleep,
        emit=lambda event, fields: _log.warning("%s %s", event, dict(fields)),
        poll_interval_s=poll_interval_s,
        scope=lambda: use_session(process_session()),
    )
    return ToolTaskService(consumer, loop_holder=loops, sleep=sleep)
