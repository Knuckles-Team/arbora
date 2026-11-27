"""Tool-action admission for ``arbora.plan/confirm`` (ARBORA-A2A-R005.3 to R005.7).

An action that follows untrusted content (egress, a privileged call or a delegation) is
admitted only by the engine. Arbora carries references: the engine produces the trust
mark and the verdict, and arbora holds no policy table and no threshold. One engine
round trip returns the verdict unchanged; a refusal never reads as an approval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

from pydantic import ConfigDict, Field
from pydantic.alias_generators import to_camel

from .models import A2APlanConfirmParams, _WireModel

ActionClass = Literal["egress", "privileged_call", "delegation"]
DEFAULT_TIMEOUT_S = 10.0
TIMEOUT_RANGE_S = (1.0, 60.0)
REFUSAL_WIRE_CODE = -32011
SHA256_HEX = r"^[0-9a-f]{64}$"


class ConfirmationKindInvalid(ValueError):
    """The confirmation kind is missing, unknown or mixed with another kind's fields."""

    code = "CONFIRMATION_KIND_INVALID"


class ConfirmRefusal(Exception):
    """One closed refusal reason; the consumer treats every refusal as deny."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ToolActionRefusal(ConfirmRefusal):
    """Why a tool action was not admitted."""


TOOL_ACTION_REASONS = frozenset(
    {
        "ENGINE_UNAVAILABLE",
        "TRUST_MARK_MISSING",
        "ACTION_CLASS_UNKNOWN",
        "GRANT_STORE_UNAVAILABLE",
        "ABSTAINED",
        "AUDIT_UNAVAILABLE",
    }
)


class ToolActionAdmissionRequest(A2APlanConfirmParams):
    """The signed confirmation of one tool action, with the engine's trust mark."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    confirmation_kind: Literal["tool_action"]
    params_digest: str = Field(pattern=SHA256_HEX)
    trust_mark_ref: str = Field(min_length=1, max_length=512)
    action_class: ActionClass


class ToolActionApproval(_WireModel):
    """The engine verdict and the references it produced."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    verdict: Literal["admit", "deny"]
    approval_ref: str = Field(min_length=1, max_length=512)
    decision_ref: str = Field(min_length=1, max_length=512)


class ToolActionDecider(Protocol):
    """One engine ``tool_action_admission`` decision; ``None`` when none was returned."""

    async def decide(
        self, request: ToolActionAdmissionRequest, *, tenant: str
    ) -> Mapping[str, Any] | None: ...


#: ``(entry) -> None``; raises when the audit entry cannot be reserved.
AuditReserve = Callable[[Mapping[str, Any]], Awaitable[None]]


def deny_without_verdict(request: ToolActionAdmissionRequest) -> ToolActionApproval:
    """The named default, applied only when the engine returns no verdict."""
    return ToolActionApproval(
        verdict="deny", approval_ref="deny_without_verdict", decision_ref="none"
    )


def bounded_timeout(configured: float | None) -> float:
    """``A2A_ENGINE_DECISION_TIMEOUT_S``: default 10, range [1, 60] seconds."""
    if configured is None:
        return DEFAULT_TIMEOUT_S
    low, high = TIMEOUT_RANGE_S
    if isinstance(configured, bool) or not low <= configured <= high:
        raise ValueError("A2A_ENGINE_DECISION_TIMEOUT_S must be within [1, 60] seconds")
    return float(configured)


async def reserve_then(
    reserve: AuditReserve,
    entry: Mapping[str, Any],
    refusal: Callable[[str], ConfirmRefusal],
) -> None:
    """Reserve the audit entry before any engine call; a failure refuses first."""
    try:
        await reserve(entry)
    except Exception:  # an unrecorded decision never proceeds
        raise refusal("AUDIT_UNAVAILABLE") from None


class ToolActionAdmitter:
    """Ask the engine once and return its verdict unchanged."""

    def __init__(
        self,
        decider: ToolActionDecider,
        audit: AuditReserve,
        *,
        timeout_s: float | None = None,
    ) -> None:
        self._decider = decider
        self._audit = audit
        self._timeout = bounded_timeout(timeout_s)

    @staticmethod
    def _entry(
        request: ToolActionAdmissionRequest, approval_ref: str, decision_ref: str
    ) -> dict[str, str]:
        # References and digests only: no credential and no parameter value.
        return {
            "approval_ref": approval_ref,
            "decision_ref": decision_ref,
            "pending_call_id": request.pending_call_id,
            "action_class": request.action_class,
            "params_digest": request.params_digest,
        }

    async def admit(
        self, request: ToolActionAdmissionRequest, *, tenant: str
    ) -> ToolActionApproval:
        if not request.trust_mark_ref.strip():
            raise ToolActionRefusal("TRUST_MARK_MISSING")
        await reserve_then(self._audit, self._entry(request, "", ""), ToolActionRefusal)
        try:
            answer = await asyncio.wait_for(
                self._decider.decide(request, tenant=tenant), self._timeout
            )
        except ConfirmRefusal:
            raise
        except Exception:  # an unreachable engine decides nothing
            raise ToolActionRefusal("ENGINE_UNAVAILABLE") from None
        if answer is None:
            approval = deny_without_verdict(request)
        elif answer.get("verdict") == "abstain":
            raise ToolActionRefusal("ABSTAINED")
        else:
            approval = ToolActionApproval.model_validate(dict(answer))
        await reserve_then(
            self._audit,
            self._entry(request, approval.approval_ref, approval.decision_ref),
            ToolActionRefusal,
        )
        return approval
