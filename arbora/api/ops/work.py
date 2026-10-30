"""Work-item and offer operations (ARBORA-OPS-R022.1).

ARBORA-OPS-R022 exposes work-item and offer operations plus evolution loop
operations, with Arbora's daemon acting only as a status/run/pause facade
while durable state remains owned by the underlying service (see
``specs/hosted-api-operations/spec.md``'s ``work.{offers.*,items.*,status}``
row). This slice (R022.1) proves the work portion -- ``work.items.list``,
``work.items.get``, and ``work.offers.create`` -- through the same
service-bound runner facade ``arbora.api.ops.decide`` already uses for
``decide.commit``; ``work.status`` and the ``evolution.*`` family are
tracked as ARBORA-OPS-R022.2.

Reads run through the bound ``work_runner`` scoped to
``context.caller.tenant``.  ``work.offers.create`` prices an offer on a Gap
(``gap_id``, ``expected_offer_version``, typed offer) under the caller's own
engine identity: the runner proves the caller holds the Gap's live WorkItem
lease (``MintWorkItemClaimCapability``) before ``WorkOfferPut``, so an offer
is bound to the principal holding the lease.  Medulla holds every record.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.registry import OpSpec, Verb


class WorkItemsListParams(Params):
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=50, ge=1, le=100)


class WorkItemRefParams(Params):
    item_id: str = Field(min_length=1, max_length=80)


class WorkOfferTerms(Params):
    """The engine ``WorkOffer``: a priced, evidence-cited offer on one Gap."""

    blast_radius: Literal["local", "repository", "fleet"]
    cooldown_until_ms: int | None = Field(default=None, ge=0)
    cost_uncertainty_ppm: int = Field(ge=0)
    depends_on_gap_ids: list[str] = Field(default_factory=list, max_length=64)
    evidence_digests: list[str] = Field(min_length=1, max_length=64)
    expected_cost_microunits: int = Field(ge=0)
    expected_utility_micros: int = Field(ge=0)
    probability_of_closure_ppm: int = Field(ge=0, le=1_000_000)
    repository_scope: list[str] = Field(default_factory=list, max_length=64)
    required_capabilities: list[str] = Field(default_factory=list, max_length=64)
    reversible: bool


class WorkOfferCreateParams(Params):
    gap_id: str = Field(min_length=1, max_length=200)
    expected_offer_version: int = Field(ge=0)
    offer: WorkOfferTerms


class WorkResult(Params):
    value: dict[str, Any]


def _bound_runner(context: Any) -> Any:
    runner = context.services.get("work_runner")
    if runner is None:
        raise RuntimeError("work runner is not bound")
    return runner


async def handle_work(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Dispatch to the service-bound work-item/offer runner; Arbora owns no state here."""
    runner = _bound_runner(context)
    if op.id == "work.items.list":
        result = await runner.list_items(
            tenant=context.caller.tenant,
            principal=context.caller.principal,
            cursor=params.get("cursor"),
            limit=params["limit"],
        )
    elif op.id == "work.items.get":
        result = await runner.get_item(
            params["item_id"],
            tenant=context.caller.tenant,
            principal=context.caller.principal,
        )
        if result is None:
            raise LookupError("work item not found")
    elif op.id == "work.offers.create":
        if not context.idempotency_key:
            raise ValueError("Idempotency-Key is required")
        # Caller-bound: the lease proof and the offer write both run under
        # the caller's own engine identity (``context.client``), never
        # Arbora's process identity, so only the live lease holder can offer.
        result = await runner.create_offer(
            context.client,
            gap_id=params["gap_id"],
            expected_offer_version=params["expected_offer_version"],
            offer=dict(params["offer"]),
            tenant=context.caller.tenant,
            idempotency_key=context.idempotency_key,
        )
    else:
        raise ValueError("unknown work operation")
    return {"value": dict(result)}


def operations() -> tuple[OpSpec, ...]:
    handler = "arbora.api.ops.work.handle_work"
    return (
        build_read_op(
            op_id="work.items.list",
            summary="List this tenant's bound work items",
            examples=("list my work items",),
            params=WorkItemsListParams,
            result=WorkResult,
            handler=handler,
            scope="work:read",
            verb=Verb.FIND,
        ),
        build_read_op(
            op_id="work.items.get",
            summary="Get one owned work item",
            examples=("show this work item",),
            params=WorkItemRefParams,
            result=WorkResult,
            handler=handler,
            scope="work:read",
        ),
        build_write_op(
            op_id="work.offers.create",
            summary="Price an offer on a Gap whose WorkItem lease the caller holds",
            examples=("offer to take this work item",),
            params=WorkOfferCreateParams,
            result=WorkResult,
            handler=handler,
            scope="work:offer-write",
        ),
    )


specs = operations

__all__ = [
    "WorkItemRefParams",
    "WorkItemsListParams",
    "WorkOfferCreateParams",
    "WorkOfferTerms",
    "WorkResult",
    "handle_work",
    "operations",
    "specs",
]
