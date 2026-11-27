"""The production finance schedule host (ARBORA-DATA-MARKET-R002).

``arbora.finance.loop_body.FinanceLoopBody`` runs on the Loops worker and needs a host
for its engine reads and job doors. This is that host, over the worker's engine
``send``. Arbora holds no schedule state: a schedule entry is the engine node
``finance-schedule:<tenant>:<loop_id>``, the owner's delegation is an engine resource grant
on ``finance-delegation:<owner>`` read with ``CheckResourceAccess`` (written by
``finance.delegation.grant``), and every run claim is an engine receipt. ``last_run`` moves only by compare-and-set from the value
the body read, so a second worker cannot move it backwards.

Job doors: ``scan`` sends the entry's stored ``request`` to the engine ``signal_scan``;
``dca_due`` reads the entry's ``dca_plan_id`` plan and runs ``run_dca_due`` over it; a paper
fill goes through the ``paper_order`` door, which production composition binds to the served
``orders.paper_submit`` operation (no second order path).
``backfill`` and ``explain`` call served operations (``ingest.run``, ``agents.run``) that
need a verified request context a worker does not hold, so they refuse ``UNAVAILABLE``
until the host binds them in ``jobs``. A door that is not bound never falls back.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import msgpack

from arbora.api.domain.finance.delegation import delegated, delegation_holds
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import decode, node_rows
from arbora.finance.dca import (
    DCAHost,
    DCAPlan,
    build_dca_proposal_key,
    minor_exponent,
    run_dca_due,
)
from arbora.finance.receipts import (
    FinanceReceiptEngine,
    ServedReceiptEngine,
    receipt_id,
)
from arbora.finance.schedule import FinanceScheduleEntry

__all__ = [
    "EngineFinanceScheduleHost",
    "schedule_node_id",
]

Send = Callable[[str, dict[str, Any]], Awaitable[Any]]
#: One job door: the stored record and the occurrence key.
JobDoor = Callable[[Any, str], Awaitable[None]]
#: The paper order door: ``(plan, order arguments, idempotency key)``.
PaperDoor = Callable[[DCAPlan, Mapping[str, Any], str], Awaitable[Any]]


def schedule_node_id(tenant: str, loop_id: str) -> str:
    return f"finance-schedule:{tenant}:{loop_id}"


async def _paper_unbound(plan: DCAPlan, arguments: Mapping[str, Any], key: str) -> Any:
    raise OperationRefused(
        "UNAVAILABLE", {"reason": "the paper order door is not bound"}
    )


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


def _price_reference_of(answer: Any) -> dict[str, Any] | None:
    reference = answer.get("price_reference") if isinstance(answer, Mapping) else None
    return dict(reference) if reference else None


class _EngineDcaHost(DCAHost):
    """A failed occurrence moves its receipt to ``failed``; paper fills need a bound door.

    A DCA plan is a notional amount and the paper order takes a quantity, so the door gets
    the notional and prices it at fill time; the price reference it answers is recorded on
    the occurrence's receipt. The fill runs under the owner's delegation, which the
    ownership check confirms with the engine's grant ledger.
    """

    def __init__(
        self,
        engine: FinanceReceiptEngine,
        paper: PaperDoor | None,
        occurrence: str,
        grant: str,
    ) -> None:
        self._engine = engine
        self._paper = paper or _paper_unbound
        self._occurrence = occurrence
        self._grant = grant

    async def submit_paper(self, plan: DCAPlan, proposal: Mapping[str, Any]) -> None:
        key = build_dca_proposal_key(plan, self._occurrence)
        answer = await self._fill(plan, proposal, key)
        await self._record_price(plan, key, answer)

    async def _fill(self, plan: DCAPlan, proposal: Mapping[str, Any], key: str) -> Any:
        notional = Decimal(int(proposal["amount_minor_units"])).scaleb(
            -minor_exponent(plan.currency)
        )
        order = {
            "account_id": plan.account_id,
            "symbol": plan.symbol,
            "side": str(proposal.get("side", "buy")).lower(),
            "notional": format(notional, "f"),
            "order_type": "market",
            "mode": "paper",
        }
        with delegated(plan.owner, self._grant, plan.account_id):
            return await self._paper(plan, order, f"dca:{key}")

    async def _record_price(self, plan: DCAPlan, key: str, answer: Any) -> None:
        reference = _price_reference_of(answer)
        if reference:
            await self._engine.annotate(
                receipt_id(plan.tenant_id, "dca_proposal", key),
                {"priceReference": reference},
            )

    async def missed(self, plan: DCAPlan, occurrence: str, reason: str) -> None:
        key = build_dca_proposal_key(plan, occurrence)
        await self._engine.move(receipt_id(plan.tenant_id, "dca_proposal", key), "fail")


class EngineFinanceScheduleHost:
    """The ``FinanceScheduleHost`` over the Loops worker's engine ``send``."""

    def __init__(
        self,
        send: Send,
        tenant: str,
        jobs: Mapping[str, JobDoor] | None = None,
        *,
        scheduler: str,
        paper_order: PaperDoor | None = None,
    ) -> None:
        self._send = send
        self._tenant = tenant
        self._scheduler = scheduler
        self._paper_order = paper_order
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
        """The engine's resource grant ledger decides; no local grant stands in."""
        return await delegation_holds(
            self._wire, self._scheduler, record.entry.owner, record.grant
        )

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
            _EngineDcaHost(engine, self._paper_order, occurrence_key, record.grant),
            plan,
            occurrence_key,
        )

    async def _wire(self, method: str, params: dict[str, Any]) -> Any:
        return decode(await self._send(method, params))
