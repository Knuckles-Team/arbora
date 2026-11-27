"""Finance application-service domain (ARBORA-DATA-MARKET-R001/R002/R003/R006).

Server-side market reads (:mod:`arbora.api.domain.finance.markets`), idempotent
finance-schedule run records (:mod:`arbora.finance.schedule`), deduplicated
flip-alert delivery (:mod:`arbora.finance.alerts`), and the recurring DCA
plan model with its non-duplicating proposal key
(:mod:`arbora.finance.dca`). All three dedupe through the engine receipt
machine (:mod:`arbora.finance.receipts`); Arbora keeps no receipt store. REST/MCP wiring, the live scheduler tick, and
the finance outbox subscription remain follow-on work (GDM-06/GDM-07/
GDM-07a/GDM-08 in ``specs/data-and-market-projections/tasks.md``).
"""

from __future__ import annotations

from arbora.api.domain.finance.markets import (
    AccountBinding,
    MarketProvider,
    MarketReadRequest,
    MarketReadResult,
    get_market_read,
)
from arbora.finance.alerts import (
    DeliveryOutcome,
    FlipAlertDelivery,
    record_alert_delivery,
)
from arbora.finance.dca import (
    DCAPlan,
    DCAProposalOutcome,
    PlanMode,
    build_dca_proposal_key,
    record_dca_proposal,
)
from arbora.finance.receipts import (
    FinanceReceiptEngine,
    ServedReceiptEngine,
    claim_receipt,
    complete_receipt,
)
from arbora.finance.schedule import (
    FinanceScheduleEntry,
    RunOutcome,
    ScheduleAction,
    record_schedule_run,
)

__all__ = [
    "AccountBinding",
    "DCAPlan",
    "DCAProposalOutcome",
    "DeliveryOutcome",
    "FinanceReceiptEngine",
    "FinanceScheduleEntry",
    "FlipAlertDelivery",
    "MarketProvider",
    "MarketReadRequest",
    "MarketReadResult",
    "PlanMode",
    "RunOutcome",
    "ScheduleAction",
    "ServedReceiptEngine",
    "claim_receipt",
    "complete_receipt",
    "build_dca_proposal_key",
    "get_market_read",
    "record_alert_delivery",
    "record_dca_proposal",
    "record_schedule_run",
]
