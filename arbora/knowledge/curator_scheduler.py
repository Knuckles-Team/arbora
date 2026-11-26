"""Run the knowledge curator on the engine cadence (ARBORA-OPS-R958).

The scheduler only ticks. For each tenant it claims the curation loop lease,
asks the engine whether a cycle is due, and starts the curator agent through
``agents.run``. It holds no cadence value and no due rule: the engine owns
``curation.cadence_seconds`` and answers ``CURATION_NOT_DUE``. The curator is a
registered agent definition, not code in arbora.

The engine refuses ``Inputs`` without a live lease, so the claim comes first and
the lease is released whenever no run starts. A lease held elsewhere ends the
tick for that tenant with no run. Time and waiting come from an injected
:class:`Clock`; nothing here sleeps on the wall clock.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.service_caller import ServiceCallers

logger = logging.getLogger(__name__)

CURATION_LOOP_ID = "knowledge-curation"
CURATOR_AGENT_ID = "knowledge-curator"
CURATE_GRANT = "knowledge:curate"
_LEASE_HELD = "LOOP_LEASE_HELD"
_NOT_DUE = "CURATION_NOT_DUE"
_LOOPS_SCOPES = frozenset({"loops:control"})
_CURATE_SCOPES = frozenset({CURATE_GRANT})

TickResult = Literal["started", "not_due", "lease_held", "failed"]


class Clock(Protocol):
    """The scheduler's only source of time and waiting."""

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class CurationPort(Protocol):
    """The engine's curation lease and inputs, as one tenant's service caller."""

    async def claim(self, tenant: str, loop_id: str) -> int | None:
        """The lease epoch, or ``None`` when a live lease is held elsewhere."""

    async def inputs(self, tenant: str, loop_id: str, epoch: int) -> bool:
        """Whether a cycle is due; ``False`` also for ``CURATION_NOT_DUE``."""

    async def release(self, tenant: str, loop_id: str, epoch: int) -> None: ...


class CuratorRunner(Protocol):
    """Starts the curator agent through ``agents.run`` under the delegated grant."""

    async def start(
        self, tenant: str, agent_id: str, *, loop_id: str, epoch: int, key: str
    ) -> str:
        """The started task id."""


@dataclass(frozen=True, slots=True)
class TickReport:
    """What one tick did for one tenant."""

    tenant: str
    result: TickResult
    run_id: str | None = None
    code: str | None = None


class CuratorScheduler:
    """Tick, claim, ask the engine, start the curator."""

    def __init__(
        self,
        *,
        tenants: Callable[[], Awaitable[Sequence[str]]],
        curation: CurationPort,
        runner: CuratorRunner,
        clock: Clock,
        tick_s: float,
        loop_id: str = CURATION_LOOP_ID,
        agent_id: str = CURATOR_AGENT_ID,
    ) -> None:
        if tick_s <= 0:
            raise ValueError("scheduler.tick_s must be positive")
        self._tenants = tenants
        self._curation = curation
        self._runner = runner
        self._clock = clock
        self._tick_s = tick_s
        self._loop_id = loop_id
        self._agent_id = agent_id

    async def tick(self) -> list[TickReport]:
        """One pass over every tenant; a tenant's failure never stops the rest."""
        return [await self._tenant_tick(tenant) for tenant in await self._tenants()]

    async def run(self, should_stop: Callable[[], bool]) -> None:
        """Tick every ``tick_s`` seconds of the injected clock until told to stop."""
        while not should_stop():
            await self.tick()
            await self._clock.sleep(self._tick_s)

    async def _tenant_tick(self, tenant: str) -> TickReport:
        epoch = await self._claim(tenant)
        if isinstance(epoch, TickReport):
            return epoch
        try:
            if not await self._curation.inputs(tenant, self._loop_id, epoch):
                await self._curation.release(tenant, self._loop_id, epoch)
                return TickReport(tenant, "not_due")
            run_id = await self._runner.start(
                tenant,
                self._agent_id,
                loop_id=self._loop_id,
                epoch=epoch,
                key=f"curation:{tenant}:{self._loop_id}:{epoch}",
            )
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            code = _code(exc)
            logger.warning("curation tick failed for a tenant: %s", code)
            await self._curation.release(tenant, self._loop_id, epoch)
            return TickReport(tenant, "failed", code=code)
        return TickReport(tenant, "started", run_id=run_id)

    async def _claim(self, tenant: str) -> int | TickReport:
        try:
            epoch = await self._curation.claim(tenant, self._loop_id)
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            code = _code(exc)
            logger.warning("curation claim failed for a tenant: %s", code)
            return TickReport(tenant, "failed", code=code)
        if epoch is None:
            return TickReport(tenant, "lease_held")
        return epoch


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", None) or type(exc).__name__)


def _engine_code(refusal: EngineRefusal) -> str:
    """The engine's own refusal code: a named leading token of the detail wins."""
    cause = refusal.__cause__
    leading = str(getattr(cause, "detail", "") or "").split(":", 1)[0].strip()
    return leading if leading.isupper() and "_" in leading else refusal.code


class EngineCuration:
    """The curation port over the engine, as each tenant's service caller."""

    def __init__(self, callers: ServiceCallers) -> None:
        self._callers = callers

    async def claim(self, tenant: str, loop_id: str) -> int | None:
        op = {"op": "claim", "loop_id": loop_id}
        try:
            answer = await self._send(
                tenant, "Loops", op, _LOOPS_SCOPES, uuid.uuid4().hex
            )
        except EngineRefusal as refusal:
            if _engine_code(refusal) == _LEASE_HELD:
                return None
            raise
        return _epoch(answer)

    async def inputs(self, tenant: str, loop_id: str, epoch: int) -> bool:
        op = {"op": "inputs", "loop_id": loop_id, "epoch": epoch, "force": False}
        try:
            answer = await self._send(
                tenant, "KnowledgeCuration", op, _CURATE_SCOPES, None
            )
        except EngineRefusal as refusal:
            if _engine_code(refusal) == _NOT_DUE:
                return False
            raise
        return bool(isinstance(answer, Mapping) and answer.get("due"))

    async def release(self, tenant: str, loop_id: str, epoch: int) -> None:
        op = {"op": "release", "loop_id": loop_id, "epoch": epoch}
        await self._send(tenant, "Loops", op, _LOOPS_SCOPES, f"{loop_id}:{epoch}")

    async def _send(
        self,
        tenant: str,
        method: str,
        op: dict[str, Any],
        scopes: frozenset[str],
        key: str | None,
    ) -> Any:
        graph = self._callers.graph
        return await self._callers.send(
            tenant,
            method,
            {"op": {**op, "graph": graph}},
            scopes=scopes,
            key=None if key is None else f"curation-{op['op']}:{tenant}:{key}",
        )


def _epoch(answer: Any) -> int:
    instance = answer.get("instance") if isinstance(answer, Mapping) else None
    lease = instance.get("lease") if isinstance(instance, Mapping) else None
    epoch = lease.get("epoch") if isinstance(lease, Mapping) else None
    if not isinstance(epoch, int):
        raise ValueError("the engine claim carried no lease epoch")
    return epoch


class AgentsRunStarter:
    """Starts the curator through the served ``agents.run`` operation.

    The call is the tenant's service caller invoking ``agents.run``. The curation
    lease epoch rides in the message metadata so the agent's ``knowledge.curation``
    calls carry it; the delegated grant is :data:`CURATE_GRANT` and nothing wider.
    """

    def __init__(self, callers: ServiceCallers) -> None:
        self._callers = callers

    async def start(
        self, tenant: str, agent_id: str, *, loop_id: str, epoch: int, key: str
    ) -> str:
        params = {
            "message": {
                "role": "user",
                "messageId": key,
                "parts": [
                    {"kind": "text", "text": "Run one knowledge curation cycle."}
                ],
                "metadata": {
                    "agent_id": agent_id,
                    "delegated_grants": [CURATE_GRANT],
                    "curation": {"loop_id": loop_id, "epoch": epoch},
                },
            }
        }
        result = await self._callers.invoke(tenant, "agents.run", params, key)
        value = result.get("value") if isinstance(result, Mapping) else None
        task = (
            value.get("task_id") or value.get("id")
            if isinstance(value, Mapping)
            else None
        )
        if not isinstance(task, str):
            raise ValueError("agents.run answered without a task id")
        return task


def build_curator_scheduler(
    callers: ServiceCallers, clock: Clock, *, tick_s: float
) -> CuratorScheduler:
    """The scheduler over one host's service callers: the host bootstrap's entry."""
    return CuratorScheduler(
        tenants=callers.tenants,
        curation=EngineCuration(callers),
        runner=AgentsRunStarter(callers),
        clock=clock,
        tick_s=tick_s,
    )
