"""Held-item admission for ``arbora.plan/confirm`` (ARBORA-A2A-R006.3 to R006.7).

The engine owns a held run output or stored run program and decides whether it is
admitted. Arbora returns the engine verdict unchanged. For a stored program it checks one
thing: the verdict must carry a ``verification_ref`` and name a verifier other than the
compiler. A refusal leaves the item held.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from .models import A2APlanConfirmParams, _WireModel
from .tool_action_admission import (
    SHA256_HEX,
    AuditReserve,
    ConfirmRefusal,
    bounded_timeout,
    reserve_then,
)

HELD_ITEM_REASONS = frozenset(
    {
        "ENGINE_UNAVAILABLE",
        "ITEM_UNKNOWN",
        "DIGEST_MISMATCH",
        "VERIFIER_NOT_INDEPENDENT",
        "PASSES_INSUFFICIENT",
        "RETIRED",
        "POLICY_EVIDENCE_MISSING",
        "ABSTAINED",
        "AUDIT_UNAVAILABLE",
    }
)
ENGINE_REFUSALS = HELD_ITEM_REASONS - {"ENGINE_UNAVAILABLE", "AUDIT_UNAVAILABLE"}


class HeldItemRefusal(ConfirmRefusal):
    """Why a held item was not admitted; the item stays held."""


class HeldItemSubject(_WireModel):
    """The held item the engine decides on: what it is, its digest and its decision.

    The confirm route signs it (:class:`HeldItemAdmissionRequest`); the critique gate
    of ``message/send`` asks about the same subject with no human in the exchange.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    item_kind: Literal["run_output_proposal", "stored_run_program"]
    item_ref: str = Field(min_length=1, max_length=512)
    content_digest: str = Field(pattern=SHA256_HEX)
    engine_decision_ref: str = Field(min_length=1, max_length=512)


class HeldItemAdmissionRequest(A2APlanConfirmParams, HeldItemSubject):
    """The signed confirmation of one held item."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    confirmation_kind: Literal["held_item"]


class HeldItemVerdict(_WireModel):
    """The engine verdict on one held item."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    outcome: Literal["admitted", "rejected", "abstained"]
    verdict_ref: str = Field(min_length=1, max_length=512)
    verification_ref: str | None = Field(default=None, max_length=512)
    verifier: str | None = Field(default=None, max_length=512)
    compiler: str | None = Field(default=None, max_length=512)
    reason: str | None = Field(default=None, max_length=64)


class HeldItemDecider(Protocol):
    """One engine ``held_item_admission`` decision."""

    async def decide(
        self, request: HeldItemSubject, *, tenant: str
    ) -> Mapping[str, Any] | None: ...


class HeldItemAdmitter:
    """Ask the engine once and return its verdict unchanged."""

    def __init__(
        self,
        decider: HeldItemDecider,
        audit: AuditReserve,
        *,
        timeout_s: float | None = None,
    ) -> None:
        self._decider = decider
        self._audit = audit
        self._timeout = bounded_timeout(timeout_s)

    @staticmethod
    def _entry(
        request: HeldItemSubject,
        verdict: HeldItemVerdict | None,
        reason: str = "",
    ) -> dict[str, str]:
        entry = {
            "verdict_ref": verdict.verdict_ref if verdict else "",
            "item_kind": request.item_kind,
            "item_ref": request.item_ref,
            "content_digest": request.content_digest,
            "verification_ref": (verdict.verification_ref or "") if verdict else "",
        }
        if reason:
            entry["reason"] = reason
        return entry

    async def admit(self, request: HeldItemSubject, *, tenant: str) -> HeldItemVerdict:
        await reserve_then(self._audit, self._entry(request, None), HeldItemRefusal)
        try:
            answer = await asyncio.wait_for(
                self._decider.decide(request, tenant=tenant), self._timeout
            )
        except ConfirmRefusal:
            raise
        except Exception:  # noqa: BLE001 - an unreachable engine decides nothing
            raise HeldItemRefusal("ENGINE_UNAVAILABLE") from None
        if answer is None:
            raise HeldItemRefusal("POLICY_EVIDENCE_MISSING")
        verdict = HeldItemVerdict.model_validate(dict(answer))
        if verdict.outcome == "abstained":
            await self._refuse(request, verdict, verdict.reason or "ABSTAINED")
        if verdict.outcome == "rejected":
            reason = (
                verdict.reason if verdict.reason in ENGINE_REFUSALS else "ABSTAINED"
            )
            await self._refuse(request, verdict, reason)
        if request.item_kind == "stored_run_program":
            if not verdict.verification_ref:
                await self._refuse(request, verdict, "POLICY_EVIDENCE_MISSING")
            if not verdict.verifier or verdict.verifier == verdict.compiler:
                await self._refuse(request, verdict, "VERIFIER_NOT_INDEPENDENT")
        await reserve_then(self._audit, self._entry(request, verdict), HeldItemRefusal)
        return verdict

    async def _refuse(
        self, request: HeldItemSubject, verdict: HeldItemVerdict, reason: str
    ) -> None:
        reason = reason if reason in HELD_ITEM_REASONS else "ABSTAINED"
        await reserve_then(
            self._audit, self._entry(request, verdict, reason), HeldItemRefusal
        )
        raise HeldItemRefusal(reason)
