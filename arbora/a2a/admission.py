"""Typed context-budget tool-subset admission for the A2A door.

Owned by ``ARBORA-A2A-R016`` to ``R020``: admission, refusal and provenance for the
tool subset the engine commits in its ``harness_config`` decision
(``MEDULLA-DECISION-ENGINE-R128``). Arbora makes one decider call per request,
holds no policy table, no default threshold and no cache, and refuses closed when
the answer is unavailable, stale, abstained or partial.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator
from pydantic.alias_generators import to_camel

from arbora.api.domain.a2a.models import A2AContextBudget, WireModel

__all__ = [
    "ModelProfilePreference",
    "ModelProfileAvailability",
    "ModelProfileRefusalReason",
    "ModelProfileUnavailable",
    "ModelProfileDecision",
    "resolve_model_profile",
    "ToolSubsetAdmissionDecision",
    "ToolSubsetAdmissionRefusalReason",
    "ToolSubsetAdmissionRequest",
    "HarnessConfigDecider",
    "HarnessConfigMirror",
    "HarnessConfigRecord",
    "HarnessPartMirror",
    "admit_tool_subset",
    "priority_for",
]

ToolSubsetAdmissionRefusalReason = Literal[
    "HARNESS_CONFIG_UNAVAILABLE",
    "HARNESS_CONFIG_ABSTAINED",
    "ENVELOPE_LACKS_ALLOWED_TOOL_SUBSET",
    "ENVELOPE_LACKS_HARNESS_CONFIG",
    "CONTEXT_BUDGET_MISSING",
]

Priority = Literal["interactive", "orchestration"]

_OPERATION = "eg.decide:harness_config"
_PART_NAMES = (
    "action_space",
    "context_policy",
    "hard_threshold",
    "plan_scaffold",
    "recent_window_fraction",
    "soft_threshold",
    "tool_subset",
)


class HarnessPartMirror(WireModel):
    """One part of the engine's ``HarnessConfigAnswer``: decided, or abstained.

    Mirrors the engine's ``Decided``/``Abstained { missing_fact, conservative_default }``
    wrapper (``MEDULLA-DECISION-ENGINE-R128.3``). Arbora never applies the default.
    """

    decision: Literal["decided", "abstained"]
    value: Any = None
    conservative_default: Any = None
    missing_fact: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _tag_name(cls, data: Any) -> Any:
        """Read the part tag under either name the engine has shipped it as.

        PIN-LAG: the pinned engine tags a part ``status``; engine ``main`` tags it
        ``decision``. The mirror accepts both and serves ``decision``.
        """
        if isinstance(data, dict) and "decision" not in data and "status" in data:
            return {
                **{k: v for k, v in data.items() if k != "status"},
                "decision": data["status"],
            }
        return data


class HarnessConfigMirror(WireModel):
    """Frozen hand-written mirror of the engine's ``HarnessConfigAnswer``."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    action_space: HarnessPartMirror
    context_policy: HarnessPartMirror
    hard_threshold: HarnessPartMirror
    plan_scaffold: HarnessPartMirror
    recent_window_fraction: HarnessPartMirror
    soft_threshold: HarnessPartMirror
    tool_subset: HarnessPartMirror

    def parts(self) -> dict[str, HarnessPartMirror]:
        return {name: getattr(self, name) for name in _PART_NAMES}


class HarnessConfigRecord(WireModel):
    """The committed engine record of one ``harness_config`` decision."""

    answer: HarnessConfigMirror
    decision_record_ref: str = Field(min_length=1, max_length=512)
    #: The record digest, 64 lowercase hex; bound into the dispatch envelope.
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Read from the engine record; Arbora holds no default lifetime.
    expires_at_ms: int | None = Field(default=None, ge=0)


@runtime_checkable
class HarnessConfigDecider(Protocol):
    """The engine decision port for the harness configuration (R017.1)."""

    async def decide(
        self,
        *,
        tenant: str,
        model_profile_ref: str,
        task_class: str,
        budget_tokens: int,
        priority: Priority,
        tool_refs: tuple[str, ...] = (),
        valid_for_ms: int | None = None,
    ) -> HarnessConfigRecord: ...


class ToolSubsetAdmissionRequest(WireModel):
    """A caller's request to admit the smallest authorized tool subset.

    ``model_profile_ref`` and ``task_class`` come from the routed capability,
    never from the caller (``ARBORA-A2A-R016.2``).
    """

    agent_graph_ref: str = Field(min_length=1, max_length=512)
    context_budget: A2AContextBudget | None = None
    model_profile_ref: str = Field(min_length=1, max_length=512)
    task_class: str = Field(min_length=1, max_length=512)
    tenant: str = Field(default="", max_length=512)
    priority: Priority = "interactive"
    #: The routed agent's library tool refs; the engine admits a subset of them.
    tool_refs: tuple[str, ...] = Field(default=(), max_length=64)
    #: The lifetime the caller's host asks the engine to seal (``valid_for_ms``);
    #: ``None`` asks for none, so the record carries no expiry.
    valid_for_ms: int | None = Field(default=None, ge=1)


class ToolSubsetAdmissionDecision(WireModel):
    """The door's admit/refuse result. Never advisory; never partial."""

    admitted: bool
    admitted_tools: tuple[str, ...] = Field(default=(), max_length=64)
    refusal_reason: ToolSubsetAdmissionRefusalReason | None = None
    provenance: str = Field(min_length=1, max_length=256)
    harness_config: HarnessConfigMirror | None = None
    decision_record_ref: str | None = Field(default=None, max_length=512)
    context_budget_tokens: int | None = Field(default=None, ge=1)
    harness_config_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _admitted_carries_all_or_none(self) -> ToolSubsetAdmissionDecision:
        carried = (
            self.harness_config,
            self.decision_record_ref,
            self.context_budget_tokens,
        )
        if self.admitted:
            if self.refusal_reason is not None or any(c is None for c in carried):
                raise ValueError("an admitted decision carries the answer, no reason")
        elif self.refusal_reason is None or any(c is not None for c in carried):
            raise ValueError("a refused decision carries a reason and no answer")
        return self


def priority_for(caller: Any) -> Priority:
    """The lease priority of a principal class; the caller never supplies it (R017.3)."""
    kind = getattr(caller, "principal_kind", None)
    if kind == "human":
        return "interactive"
    if kind == "service":
        return "orchestration"
    raise PermissionError("principal class has no admission priority")


def _refused(
    reason: ToolSubsetAdmissionRefusalReason, provenance: str
) -> ToolSubsetAdmissionDecision:
    return ToolSubsetAdmissionDecision(
        admitted=False,
        refusal_reason=reason,
        provenance=provenance[:256],
    )


def _abstentions(answer: HarnessConfigMirror) -> list[str]:
    return [
        f"{name}:{'+'.join(part.missing_fact) or 'unknown'}={part.conservative_default!s}"
        for name, part in answer.parts().items()
        if part.decision == "abstained"
    ]


def _incomplete(answer: HarnessConfigMirror) -> bool:
    """A summarizing policy needs both thresholds decided (R020.2)."""
    if answer.context_policy.value != "elide_then_summarize":
        return False
    return any(
        part.decision != "decided" or part.value is None
        for part in (answer.soft_threshold, answer.hard_threshold)
    )


def _envelope_gap(dispatch_model: Any) -> ToolSubsetAdmissionRefusalReason | None:
    fields = getattr(dispatch_model, "model_fields", {})
    if "allowed_tool_subset" not in fields:
        return "ENVELOPE_LACKS_ALLOWED_TOOL_SUBSET"
    if "decision_record_ref" not in fields or "harness_config_digest" not in fields:
        return "ENVELOPE_LACKS_HARNESS_CONFIG"
    return None


def _lifetime(request: ToolSubsetAdmissionRequest) -> dict[str, int]:
    """``valid_for_ms`` only when a host asked for one: no default is invented."""
    return (
        {} if request.valid_for_ms is None else {"valid_for_ms": request.valid_for_ms}
    )


async def admit_tool_subset(
    request: ToolSubsetAdmissionRequest,
    decider: HarnessConfigDecider,
    *,
    clock: Callable[[], float] = time.time,
    dispatch_model: Any = None,
) -> ToolSubsetAdmissionDecision:
    """Admit the engine's committed tool subset, or refuse closed with provenance.

    One decider call per request and no cache: an earlier subset, an advisory
    one or a trimmed one is never returned (``ARBORA-A2A-R017.2``, ``R020``).
    """
    tokens = request.context_budget.tokens if request.context_budget else None
    if tokens is None or tokens < 1:
        return _refused("CONTEXT_BUDGET_MISSING", f"{_OPERATION}:budget_missing")
    try:
        record = await decider.decide(
            tenant=request.tenant,
            model_profile_ref=request.model_profile_ref,
            task_class=request.task_class,
            budget_tokens=tokens,
            priority=request.priority,
            tool_refs=request.tool_refs,
            **_lifetime(request),
        )
    except Exception:
        logging.getLogger(__name__).debug(
            "an unanswered question admits nothing", exc_info=True
        )
        return _refused("HARNESS_CONFIG_UNAVAILABLE", f"{_OPERATION}:unavailable")
    if record.expires_at_ms is not None and clock() * 1000 >= record.expires_at_ms:
        return _refused("HARNESS_CONFIG_UNAVAILABLE", f"{_OPERATION}:stale")
    abstained = _abstentions(record.answer)
    if abstained:
        return _refused(
            "HARNESS_CONFIG_ABSTAINED", f"{_OPERATION}:abstained:{';'.join(abstained)}"
        )
    tools = record.answer.tool_subset.value
    if _incomplete(record.answer) or not isinstance(tools, (list, tuple)):
        return _refused("HARNESS_CONFIG_UNAVAILABLE", f"{_OPERATION}:partial")
    if len(tools) > 64 or any(not isinstance(tool, str) or not tool for tool in tools):
        return _refused("HARNESS_CONFIG_UNAVAILABLE", f"{_OPERATION}:partial")
    gap = _envelope_gap(dispatch_model or _dispatch_model())
    if gap is not None:
        return _refused(gap, f"{_OPERATION}:{gap.lower()}")
    return ToolSubsetAdmissionDecision(
        admitted=True,
        admitted_tools=tuple(tools),
        provenance=f"{_OPERATION}:{record.decision_record_ref}"[:256],
        harness_config=record.answer,
        decision_record_ref=record.decision_record_ref,
        context_budget_tokens=tokens,
        harness_config_digest=record.digest,
    )


def _dispatch_model() -> Any:
    from arbora.api.contracts import SignedAgentDispatchRequest

    return SignedAgentDispatchRequest


ModelProfileRefusalReason = Literal[
    "AUTHORITY_UNAVAILABLE",
    "CALLER_CHANGED",
    "PROFILE_REVOKED",
    "CAPABILITY_UNAVAILABLE",
    "STALE_DECISION",
    "INVALID_DECISION",
]


class ModelProfilePreference(WireModel):
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


class ModelProfileAvailability(StrEnum):
    """The one availability a closed profile refusal states."""

    UNAVAILABLE = "unavailable"


class ModelProfileUnavailable(WireModel):
    """Closed refusal without profile data, credentials or fabricated receipts."""

    status: ModelProfileAvailability = ModelProfileAvailability.UNAVAILABLE
    reason: ModelProfileRefusalReason = "AUTHORITY_UNAVAILABLE"


#: The engine ``Decide`` question this module asks (MEDULLA-DECISION-ENGINE-R039.3).
MODEL_ROUTE_QUESTION_ID = "a2a.model_profile"
PREFERENCE_NOT_LEGAL = "PREFERENCE_NOT_LEGAL"

Decide = Callable[[dict[str, Any]], Awaitable[Any]]


class ModelProfileDecision(WireModel):
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


def _premises(record: Mapping[str, Any]) -> list[Any]:
    premises = record.get("premises")
    return premises if isinstance(premises, list) else []


def _not_honoured(premise: Any, preferred: str) -> bool:
    return (
        isinstance(premise, Mapping)
        and premise.get("subject") == PREFERENCE_NOT_LEGAL
        and premise.get("fact") == f"preference_not_honoured:{preferred}"
    )


def _preference_refused(record: Mapping[str, Any], preferred: str) -> bool:
    """Whether a premise of the record is the engine's ``preference_not_honoured``.

    The route family seals the premise ``{subject: "PREFERENCE_NOT_LEGAL", fact:
    "preference_not_honoured:<option>"}`` (MEDULLA-DECISION-ENGINE-R039.3).
    """
    return any(_not_honoured(premise, preferred) for premise in _premises(record))


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

    from arbora.api.domain.deployment.placement import check_receipt, single_record

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
    except Exception:
        logging.getLogger(__name__).debug(
            "an unanswered question grants no profile", exc_info=True
        )
        return ModelProfileUnavailable(reason="INVALID_DECISION")
    return ModelProfileDecision(
        profile=chosen,
        preference_honoured=chosen == preferred
        and not _preference_refused(record, preferred),
    )
