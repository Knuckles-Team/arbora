"""Typed context-budget tool-subset admission for the A2A door.

Owned by ``ARBORA-A2A-002``: admission, refusal, and provenance for the
smallest authorized tool subset that covers a selected agent graph within a
caller's context budget (see ``docs/a2a.md``, "Tool-subset assembly
status"). This fails closed today because medulla's
``AgentAssemble`` operation returns unavailable and the signed AU
``AgentTurnEnvelope`` carries no allowed-tool-subset field
(meristem ``MERISTEM-CONTROL-R035``). Neither upstream gap is bridged with a
local heuristic, a cached prior subset, or an advisory best-effort answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, StrictStr, field_validator

from .models import A2AContextBudget, _WireModel

__all__ = [
    "ModelProfilePreference",
    "ModelProfileRefusalReason",
    "ModelProfileUnavailable",
    "ModelProfileDecision",
    "resolve_model_profile",
    "ToolSubsetAdmissionDecision",
    "ToolSubsetAdmissionRefusalReason",
    "ToolSubsetAdmissionRequest",
    "admit_tool_subset",
]

ToolSubsetAdmissionRefusalReason = Literal[
    "EG_ASSEMBLE_UNAVAILABLE",
    "ENVELOPE_LACKS_ALLOWED_TOOL_SUBSET",
]


class ToolSubsetAdmissionRequest(_WireModel):
    """A caller's request to admit the smallest authorized tool subset."""

    agent_graph_ref: str = Field(min_length=1, max_length=512)
    context_budget: A2AContextBudget


class ToolSubsetAdmissionDecision(_WireModel):
    """The door's admit/refuse result. Never advisory; never partial."""

    admitted: bool
    admitted_tools: tuple[str, ...] = Field(default=(), max_length=64)
    refusal_reason: ToolSubsetAdmissionRefusalReason | None = None
    provenance: str = Field(min_length=1, max_length=256)


def admit_tool_subset(
    request: ToolSubsetAdmissionRequest,
) -> ToolSubsetAdmissionDecision:
    """Admit the request, or fail closed with a typed, provenanced refusal.

    ``ARBORA-A2A-R017.1``: medulla's ``AgentAssemble`` operation
    returns unavailable today, so no context-budget-bound tool subset can be
    derived or cryptographically carried. This refuses closed rather than
    returning an advisory, cached, or partial subset.
    """

    del request
    return ToolSubsetAdmissionDecision(
        admitted=False,
        admitted_tools=(),
        refusal_reason="EG_ASSEMBLE_UNAVAILABLE",
        provenance="eg.AgentAssemble:unavailable",
    )


ModelProfileRefusalReason = Literal[
    "AUTHORITY_UNAVAILABLE",
    "CALLER_CHANGED",
    "PROFILE_REVOKED",
    "CAPABILITY_UNAVAILABLE",
    "STALE_DECISION",
    "INVALID_DECISION",
]


class ModelProfilePreference(_WireModel):
    """Untrusted preference only; this value conveys no execution authority."""

    preferred_profile_ref: StrictStr | None = Field(default=None, max_length=512)

    @field_validator("preferred_profile_ref")
    @classmethod
    def _printable_reference(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or any(not character.isprintable() for character in value)
        ):
            raise ValueError("profile reference must be nonempty printable text")
        return value


class ModelProfileUnavailable(_WireModel):
    """Closed refusal without profile data, credentials or fabricated receipts."""

    status: Literal["unavailable"] = "unavailable"
    reason: ModelProfileRefusalReason = "AUTHORITY_UNAVAILABLE"


#: The engine ``Decide`` question this module asks (MEDULLA-DECISION-ENGINE-R039.3).
MODEL_ROUTE_QUESTION_ID = "a2a.model_profile"
PREFERENCE_NOT_LEGAL = "PREFERENCE_NOT_LEGAL"

Decide = Callable[[dict[str, Any]], Awaitable[Any]]


class ModelProfileDecision(_WireModel):
    """The profile the engine chose and whether the caller's preference held."""

    profile: str = Field(min_length=1, max_length=512)
    preference_honoured: bool


def _chosen_profile(record: Mapping[str, Any]) -> str:
    outcome = record.get("outcome")
    scores = outcome.get("scores") if isinstance(outcome, Mapping) else None
    first = scores[0] if isinstance(scores, list) and scores else None
    option = first.get("option_id") if isinstance(first, Mapping) else None
    if not isinstance(option, str) or not option:
        raise ValueError("the engine chose no model profile")
    return option


def _preference_refused(record: Mapping[str, Any], preferred: str) -> bool:
    """Whether the record carries the engine's ``preference_not_honoured`` premise."""
    needle = f"preference_not_honoured:{preferred}"
    return needle in repr(record) or PREFERENCE_NOT_LEGAL in repr(record)


async def resolve_model_profile(
    decide: Decide | None,
    preference: ModelProfilePreference,
    *,
    tenant: str,
    principal: str,
    profiles: Sequence[str],
    feature_schema: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
) -> ModelProfileDecision | ModelProfileUnavailable:
    """Send the preference to the engine ``Route`` question; Arbora decides nothing.

    ``profiles`` are the profiles the caller may prefer (``models.list``). The
    engine ranks them, honours a legal preference and records
    ``PREFERENCE_NOT_LEGAL`` otherwise. Without a composed ``decide`` port, or
    on any engine failure, the answer stays the typed ``AUTHORITY_UNAVAILABLE``
    refusal; no local choice is made.
    """
    preferred = preference.preferred_profile_ref
    if decide is None or preferred is None or not profiles:
        return ModelProfileUnavailable()
    from medulla.decision_client import DeclaredOption, decide_request, declared, param

    from arbora.deployment.placement import check_receipt, single_record

    request = decide_request(
        tenant,
        (MODEL_ROUTE_QUESTION_ID, "route", "ordinary"),
        declared([DeclaredOption(name) for name in profiles]),
        feature_schema,
        policy=policy,
        params=[param("route.preferred_option", "text", preferred)],
    )
    try:
        record = single_record(await decide(request))
        check_receipt(record, request, principal)
        chosen = _chosen_profile(record)
    except Exception:  # noqa: BLE001 - an unanswered question grants no profile
        return ModelProfileUnavailable(reason="INVALID_DECISION")
    return ModelProfileDecision(
        profile=chosen,
        preference_honoured=chosen == preferred
        and not _preference_refused(record, preferred),
    )
