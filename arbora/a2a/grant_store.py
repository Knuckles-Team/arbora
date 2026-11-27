"""Human grants over the engine's control leases, consumed at most once.

Arbora holds no grant state. Each method calls the engine control-lease methods
(``IssueControlLease``, ``GetControlLease``, ``TransitionControlLease``) through
``client.control_leases``, as ``arbora/api/invoke/plan.py::EgPlanStore`` does for
plans. The engine consumes the grant, and the plan lease and the claimed
WorkItem it was registered with, in one transaction, so a second consume of the
same grant finds it already consumed.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from arbora.api.invoke.plan import PLAN_TTL_MS, PlanBinding

from .approval import PendingCallBinding

logger = logging.getLogger(__name__)


class GrantRefusal(StrEnum):
    """The closed refusal codes of the grant store."""

    EXPIRED = "GRANT_EXPIRED"
    REVOKED = "GRANT_REVOKED"
    REPLAYED = "GRANT_REPLAYED"
    STORE_UNAVAILABLE = "GRANT_STORE_UNAVAILABLE"


class GrantError(Exception):
    """A refused grant operation; the code is the only thing it names."""

    def __init__(self, code: GrantRefusal) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class GrantEvidence:
    """What the audit entry of an outcome records, and no value of the call."""

    grant_id: str
    grant_revision: int
    pending_call_id: str
    outcome: str


@dataclass(frozen=True, slots=True)
class GrantView:
    """One grant as the engine holds it."""

    grant_id: str
    status: str
    revision: int
    binding: PendingCallBinding

    def evidence(self, outcome: str) -> GrantEvidence:
        return GrantEvidence(
            self.grant_id, self.revision, self.binding.pending_call_id, outcome
        )


_STATUS_REFUSAL = MappingProxyType(
    {
        "consumed": GrantRefusal.REPLAYED,
        "revoked": GrantRefusal.REVOKED,
        "expired": GrantRefusal.EXPIRED,
    }
)


class HumanGrantStore:
    """Issue, read, revoke and consume human grants through the engine."""

    def __init__(
        self,
        client: Any,
        *,
        lease_kind: str,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not isinstance(lease_kind, str) or not lease_kind:
            raise ValueError("a configured grant lease kind is required")
        self._leases = client.control_leases
        self._kind = lease_kind
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    async def _engine(self, call: Callable[[], Any]) -> Any:
        try:
            return await call()
        except Exception:
            logger.warning("engine failed", exc_info=True)
            # Fail closed: an unreachable or erroring engine grants nothing.
            raise GrantError(GrantRefusal.STORE_UNAVAILABLE) from None

    async def issue(
        self,
        binding: PendingCallBinding,
        *,
        registration: Mapping[str, Any] | None = None,
    ) -> GrantView:
        """Issue the grant of ``binding``; its lifetime never exceeds ``PLAN_TTL_MS``."""
        now = self._clock_ms()
        expires = min(binding.expires_at_ms, now + PLAN_TTL_MS)
        if expires <= now:
            raise GrantError(GrantRefusal.EXPIRED)
        answer = await self._engine(
            lambda: self._leases.issue(
                tenant=binding.plan.tenant,
                lease_id=binding.grant_id,
                kind=self._kind,
                grant=binding.model_dump(mode="json"),
                issued_at_ms=now,
                expires_at_ms=expires,
                hard_expires_at_ms=expires,
                idempotency_key=f"grant-issue:{binding.grant_id}",
                pending_call_registration=(
                    dict(registration) if registration is not None else None
                ),
            )
        )
        if answer.get("outcome") != "issued":
            raise GrantError(GrantRefusal.REPLAYED)
        return await self._read(binding.plan.tenant, binding.grant_id)

    async def get(self, tenant: str, grant_id: str) -> GrantView | None:
        """The grant, or ``None``: a foreign or absent grant answers alike."""
        lease = await self._engine(
            lambda: self._leases.get(tenant=tenant, lease_id=grant_id)
        )
        return self._view(lease)

    async def revoke(self, tenant: str, grant_id: str) -> GrantEvidence:
        """Revoke an active grant."""
        view = await self._live(tenant, grant_id)
        answer = await self._transition(tenant, view, "revoked", None)
        if answer.get("outcome") != "applied":
            raise GrantError(self._refusal_of(answer))
        return view.evidence("revoked")

    async def consume(
        self,
        tenant: str,
        grant_id: str,
        *,
        consumption: Mapping[str, Any] | None = None,
    ) -> GrantEvidence:
        """Consume the grant once, with its registered pair when it carries one."""
        view = await self._live(tenant, grant_id)
        answer = await self._transition(
            tenant,
            view,
            "consumed",
            dict(consumption) if consumption is not None else None,
        )
        if answer.get("outcome") != "applied":
            raise GrantError(self._refusal_of(answer))
        return view.evidence("consumed")

    async def _read(self, tenant: str, grant_id: str) -> GrantView:
        view = await self.get(tenant, grant_id)
        if view is None:
            raise GrantError(GrantRefusal.STORE_UNAVAILABLE)
        return view

    async def _live(self, tenant: str, grant_id: str) -> GrantView:
        view = await self.get(tenant, grant_id)
        if view is None:
            raise GrantError(GrantRefusal.EXPIRED)
        if view.status != "active":
            raise GrantError(_STATUS_REFUSAL[view.status])
        if view.binding.expires_at_ms <= self._clock_ms():
            raise GrantError(GrantRefusal.EXPIRED)
        return view

    async def _transition(
        self,
        tenant: str,
        view: GrantView,
        to: str,
        consumption: dict[str, Any] | None,
    ) -> Mapping[str, Any]:
        # A fresh key per attempt: a replayed key would answer the first outcome
        # again, and consume-once is the engine's revision compare-and-set.
        key = f"grant-{to}:{view.grant_id}:{view.revision}:{secrets.token_hex(8)}"
        extra = {"pending_call_consumption": consumption} if consumption else {}
        return await self._engine(
            lambda: self._leases.transition(
                tenant=tenant,
                lease_id=view.grant_id,
                expected_revision=view.revision,
                to=to,
                idempotency_key=key,
                **extra,
            )
        )

    @staticmethod
    def _refusal_of(answer: Mapping[str, Any]) -> GrantRefusal:
        current = answer.get("lease")
        status = current.get("status") if isinstance(current, Mapping) else None
        return _STATUS_REFUSAL.get(str(status), GrantRefusal.REPLAYED)

    def _view(self, lease: Any) -> GrantView | None:
        if not isinstance(lease, Mapping) or lease.get("kind") != self._kind:
            return None
        try:
            grant = dict(lease["grant"])
            # The binding model is strict: the plan is a dataclass, not a mapping.
            grant["plan"] = PlanBinding(**grant["plan"])
            binding = PendingCallBinding.model_validate(grant)
            return GrantView(
                binding.grant_id,
                str(lease["status"]),
                int(lease["revision"]),
                binding,
            )
        except (KeyError, TypeError, ValueError):
            raise GrantError(GrantRefusal.STORE_UNAVAILABLE) from None
