"""The production finance schedule host (ARBORA-DATA-MARKET-R002).

``arbora.finance.loop_body.FinanceLoopBody`` runs on the Loops worker and needs a host
for its engine reads and job doors. This is that host, over the worker's engine
``send``. Arbora holds no schedule state: a schedule entry is the engine node
``finance-schedule:<tenant>:<loop_id>``, the owner's delegated grant is the engine node
``finance-delegation:<tenant>:<owner>:<grant>`` (``active`` true while held), and every run
claim is an engine receipt. ``last_run`` moves only by compare-and-set from the value
the body read, so a second worker cannot move it backwards.

Job doors: ``scan`` sends the entry's stored ``request`` to the engine ``signal_scan``;
``dca_due`` reads the entry's ``dca_plan_id`` plan and runs ``run_dca_due`` over it.
``backfill`` and ``explain`` call served operations (``ingest.run``, ``agents.run``) that
need a verified request context a worker does not hold, so they refuse ``UNAVAILABLE``
until the host binds them in ``jobs``. A door that is not bound never falls back.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import msgpack

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._engine import decode, node_rows
from arbora.finance.dca import DCAHost, DCAPlan, run_dca_due
from arbora.finance.receipts import (
    FinanceReceiptEngine,
    ServedReceiptEngine,
    receipt_id,
)
from arbora.finance.schedule import FinanceScheduleEntry

__all__ = [
    "EngineFinanceScheduleHost",
    "delegation_node_id",
    "schedule_node_id",
]

Send = Callable[[str, dict[str, Any]], Awaitable[Any]]
#: One job door: the stored record and the occurrence key.
JobDoor = Callable[[Any, str], Awaitable[None]]


def schedule_node_id(tenant: str, loop_id: str) -> str:
    return f"finance-schedule:{tenant}:{loop_id}"


def delegation_node_id(tenant: str, owner: str, grant: str) -> str:
    return f"finance-delegation:{tenant}:{owner}:{grant}"


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


class _EngineDcaHost(DCAHost):
    """A failed occurrence moves its receipt to ``failed``; paper fills need a bound door."""

    def __init__(self, engine: FinanceReceiptEngine) -> None:
        self._engine = engine

    async def submit_paper(self, plan: DCAPlan, proposal: Mapping[str, Any]) -> None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the paper order door is not bound"}
        )

    async def missed(self, plan: DCAPlan, occurrence: str, reason: str) -> None:
        from arbora.finance.dca import build_dca_proposal_key

        key = build_dca_proposal_key(plan, occurrence)
        await self._engine.move(receipt_id(plan.tenant_id, "dca_proposal", key), "fail")


class EngineFinanceScheduleHost:
    """The ``FinanceScheduleHost`` over the Loops worker's engine ``send``."""

    def __init__(
        self,
        send: Send,
        tenant: str,
        jobs: Mapping[str, JobDoor] | None = None,
    ) -> None:
        self._send = send
        self._tenant = tenant
        self._jobs: dict[str, JobDoor] = {
            "scan": self._scan,
            "dca_due": self._dca_due,
            **(jobs or {}),
        }

    async def _node(self, node_id: str) -> dict[str, Any] | None:
        rows = node_rows(
            await self._send("GetNodePropertiesBatch", {"node_ids": [node_id]})
        )
        for found, props in rows:
            # A foreign node answers exactly like an absent one.
            if found == node_id and props.get("tenant_id") == self._tenant:
                return props
        return None

    async def record(self, loop_id: str) -> Any:
        props = await self._node(schedule_node_id(self._tenant, loop_id))
        if props is None:
            return None
        try:
            entry = FinanceScheduleEntry(
                tenant_id=self._tenant,
                owner=props["owner"],
                action=props["action"],
                timezone=props["timezone"],
                trigger=props["trigger"],
                idempotency_key=loop_id,
            )
            last_run = datetime.fromisoformat(props["last_run"])
        except (KeyError, ValueError):
            return None
        return SimpleNamespace(
            entry_id=loop_id,
            grant=str(props.get("grant", "")),
            last_run=last_run if last_run.tzinfo else last_run.replace(tzinfo=UTC),
            entry=entry,
            props=props,
        )

    async def owner_holds(self, record: Any) -> bool:
        entry = record.entry
        held = await self._node(
            delegation_node_id(self._tenant, entry.owner, record.grant)
        )
        return bool(held and held.get("active") is True)

    def receipts(self, record: Any) -> FinanceReceiptEngine:
        return ServedReceiptEngine(self._send)

    async def run_job(self, record: Any, occurrence_key: str) -> None:
        door = self._jobs.get(record.entry.action)
        if door is None:
            raise OperationRefused(
                "UNAVAILABLE",
                {"reason": f"the {record.entry.action} job door is not bound"},
            )
        await door(record, occurrence_key)

    async def mark_run(self, record: Any, last_run: datetime) -> None:
        await self._send(
            "CompareAndSetNodeFields",
            {
                "node_id": schedule_node_id(self._tenant, record.entry_id),
                "conditions_msgpack": _pack({"last_run": record.props["last_run"]}),
                "updates_msgpack": _pack({"last_run": last_run.isoformat()}),
            },
        )

    async def _scan(self, record: Any, occurrence_key: str) -> None:
        request = record.props.get("request")
        if not isinstance(request, Mapping):
            raise OperationRefused("INVALID_ARGUMENT", {"field": "request"})
        await self._send(
            "FinanceMarket", {"op": {"op": "signal_scan", "request": dict(request)}}
        )

    async def _dca_due(self, record: Any, occurrence_key: str) -> None:
        plan_id = record.props.get("dca_plan_id")
        node = f"dca-plan:{self._tenant}:{plan_id}"
        plan_props = await self._node(node)
        if plan_props is None:
            raise OperationRefused("NOT_FOUND")
        plan = DCAPlan.model_validate(
            {k: v for k, v in plan_props.items() if k in DCAPlan.model_fields}
        )
        engine = ServedReceiptEngine(self._send)
        await run_dca_due(
            engine,
            self._wire,
            _EngineDcaHost(engine),
            plan,
            occurrence_key,
        )

    async def _wire(self, method: str, params: dict[str, Any]) -> Any:
        return decode(await self._send(method, params))
