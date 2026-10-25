"""Opt-in final-answer ACP projection over the governed dependency-run owner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import uuid4

import acp
from pydantic_ai.messages import PartStartEvent, TextPart

from arbora.a2a.models import A2ATask
from arbora.agent_runs import native_stream_scope
from arbora.api.contracts.session import use_session
from arbora.api.invoke import OpError, invoke
from arbora.api.registry import Surface

from .adapter import HostedACPAgent
from .session_policy import AcpSessionPolicy, PrincipalContext


def _committed_answer(result: Any, receipt: Any) -> str:
    value = A2ATask.model_validate(_task(result))
    if receipt is None or value.id != receipt.task_id:
        raise RuntimeError("Central ACP task identity unavailable")
    if value.metadata.get("arbora", {}).get("runId") != receipt.run_id:
        raise RuntimeError("Central ACP run identity unavailable")
    if value.status.state != "completed":
        raise acp.RequestError(
            -32001,
            "Central task is not complete",
            {"task_id": value.id, "state": value.status.state},
        )
    if not value.artifacts:
        raise acp.RequestError(
            -32001, "Committed answer unavailable", {"task_id": value.id}
        )
    # Only the owner-confirmed terminal answer, never intermediate
    # dependency-node messages or private native model events.
    answer = "\n".join(
        part.text for artifact in value.artifacts for part in artifact.parts
    )
    return answer


class CentralACPFactory:
    """Own bounded invocation tasks, not an agent loop, history or admission.

    A pre-admission disconnect does not abandon the owner operation: its later
    admission callback cancels before execution. Until that settles, the task
    still consumes capacity. No automatic retries or continuation are offered.
    """

    def __init__(
        self, invocation: Any, *, trusted_roots: frozenset[str], max_operations: int = 8
    ) -> None:
        if invocation is None or invocation.task_plan_config is None:
            raise RuntimeError("Native task-plan owner configuration required")
        if not trusted_roots or type(max_operations) is not int or max_operations < 1:
            raise ValueError("explicit trusted roots and positive capacity required")
        invocation.validate()
        required = ("agents.run", "agents.tasks.cancel", "agents.tasks.get")
        if any(invocation.services.registry.get(op) is None for op in required):
            raise RuntimeError("Governed task operations unavailable")
        service = getattr(invocation.services.runtime, "bindings", {}).get("a2a")
        owner = getattr(service, "authority", None)
        owner_invocation: Any = getattr(owner, "hosted_invocation", None)
        if (
            getattr(owner, "task_plan_config", None) != invocation.task_plan_config
            or owner_invocation is not invocation
        ):
            raise RuntimeError("Matching configured native task owner required")
        self.invocation: Any = invocation
        self.roots = trusted_roots
        self.capacity = max_operations
        self.operations: set[asyncio.Task[Any]] = set()
        self.closing = False

    def _start(self, operation: Any) -> asyncio.Task[Any]:
        if self.closing or len(self.operations) >= self.capacity:
            operation.close()
            raise RuntimeError("Central ACP capacity unavailable")
        task = asyncio.create_task(operation)
        self.operations.add(task)

        def finished(value: asyncio.Task[Any]) -> None:
            self.operations.discard(value)
            if not value.cancelled():
                value.exception()  # Consume failures without publishing private detail.

        task.add_done_callback(finished)
        return task

    async def __call__(
        self,
        principal: Callable[[], PrincipalContext | None],
        *,
        request: Any,
        authority: Any,
    ) -> HostedACPAgent:
        from arbora.security.settings import security_settings

        from .http_transport import _authorization_profile

        if not security_settings().auth_jwt_issuer:
            raise PermissionError("Configured central bearer issuer required")
        bound_profile = _authorization_profile(authority)
        current: list[Any] = []

        async def admit(request: Any, authority: Any) -> None:
            caller = await self.invocation.authenticator.authenticate(request)
            if _authorization_profile(authority) != bound_profile:
                raise PermissionError("Connection authorization profile changed")
            if caller.session != authority or not caller.authenticated:
                raise PermissionError("Exact verified HTTP authority required")
            if not {"kg:read", "kg:write"} <= caller.effective_scopes:
                raise PermissionError("Task execution and readback scopes required")
            if current and replace(caller, request_id="") != replace(
                current[0], request_id=""
            ):
                raise PermissionError("Verified operation authority changed")
            current[:] = [caller]

        await admit(request, authority)
        used: set[str] = set()

        async def turn(session_id: str, text: str, observe: Any) -> None:
            if session_id in used:
                raise RuntimeError("Central ACP continuation unavailable")
            used.add(session_id)
            await self._turn(lambda: current[0], text, observe)

        async def closed(session_id: str) -> None:
            used.discard(session_id)

        return HostedACPAgent(
            principal,
            policy=AcpSessionPolicy(timedelta(minutes=5), timedelta(minutes=10)),
            trusted_roots=self.roots,
            hosted_turn=turn,
            closed=closed,
            transport_admission=admit,
            max_prompt_bytes=16384,
            max_output_bytes=65536,
        )

    async def _turn(self, current: Callable[[], Any], text: str, observe: Any) -> None:
        caller = current()
        receipt: Any = None
        admitted = asyncio.Event()
        cancel_requested = False
        key = "acp-" + uuid4().hex

        def on_admitted(value: Any) -> None:
            nonlocal receipt
            receipt = value
            admitted.set()
            if cancel_requested:
                raise asyncio.CancelledError()

        with (
            use_session(caller.session),
            native_stream_scope(
                caller.session, None, on_admitted=on_admitted, protocol="acp"
            ),
        ):
            operation = self._start(
                invoke(
                    "agents.run",
                    {
                        "message": {
                            "role": "user",
                            "messageId": key,
                            "parts": [{"kind": "text", "text": text}],
                        }
                    },
                    caller,
                    Surface.HTTP,
                    services=self.invocation.services,
                    idempotency_key=key,
                )
            )
            waiting = asyncio.create_task(admitted.wait())
            try:
                # Waiting on this set never cancels an in-flight admission.
                await asyncio.wait(
                    (operation, waiting), return_when=asyncio.FIRST_COMPLETED
                )
                result = await operation
                answer = _committed_answer(result, receipt)
                current().session.ensure_authority_current()
                await observe(PartStartEvent(index=0, part=TextPart(answer)))
            except asyncio.CancelledError:
                cancel_requested = True
                try:
                    async with asyncio.timeout(5):
                        if receipt is None:
                            await admitted.wait()
                        cancellation_caller = current()
                        with use_session(cancellation_caller.session):
                            await self._cancel(cancellation_caller, receipt.task_id)
                        operation.cancel()
                        await asyncio.gather(operation, return_exceptions=True)
                except (Exception, asyncio.CancelledError):
                    # An unfinished admission remains owned and bounded by the
                    # factory. Its callback will request owner cancellation.
                    details = {"reason": "cancellation_unconfirmed"}
                    if receipt is not None:
                        details["task_id"] = receipt.task_id
                    raise acp.RequestError(
                        -32001, "Central cancellation outcome unknown", details
                    ) from None
                raise
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    async def _cancel(self, caller: Any, task_id: str) -> None:
        for op in ("agents.tasks.cancel", "agents.tasks.get"):
            result = await invoke(
                op,
                {"task_id": task_id},
                caller,
                Surface.HTTP,
                services=self.invocation.services,
                idempotency_key="acp-cancel-" + task_id,
            )
            value = _task(result)
            if (
                value.get("id") != task_id
                or value.get("status", {}).get("state") != "canceled"
            ):
                raise RuntimeError("Central ACP cancellation unconfirmed")

    async def aclose(self) -> None:
        self.closing = True
        tasks = tuple(self.operations)
        if tasks:
            # Carrier teardown requests governed cancellation first. Do not
            # cancel a pending admission coroutine: its submit thread may still
            # commit, and its receipt callback must remain able to settle it.
            _, pending = await asyncio.wait(tasks, timeout=5)
            if pending:
                raise RuntimeError("Central ACP shutdown outcome unknown")


def _task(result: Any) -> dict[str, Any]:
    if isinstance(result, OpError):
        raise acp.RequestError(
            -32001, "Central operation refused", {"reason": result.code}
        )
    outer = getattr(result, "value", None)
    value = outer.get("value") if isinstance(outer, dict) else None
    if not isinstance(value, dict):
        raise RuntimeError("Central ACP task outcome unavailable")
    return value


def configure_central_acp(
    app: Any, invocation: Any, *, trusted_roots: frozenset[str]
) -> None:
    """Opt in through the trusted host; shared gateway owns route registration."""
    if getattr(app.state, "acp_agent_factory", None) is not None:
        raise RuntimeError("ACP host composition already configured")
    factory = CentralACPFactory(invocation, trusted_roots=trusted_roots)
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any):
        try:
            async with previous(application) as state:
                yield state
        finally:
            # Drain carrier/adapter cancellation before bounded observation of
            # remaining admissions. Never sever their settlement callbacks.
            await factory.aclose()

    app.state.acp_agent_factory = factory
    app.router.lifespan_context = lifespan
