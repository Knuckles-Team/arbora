"""Governed agent routing for inbound A2A messages."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from .models import A2AMessage, A2ARouteDecision

logger = logging.getLogger(__name__)


__all__ = [
    "A2AAssemblyUnavailable",
    "A2AOperationBridgeUnavailable",
    "A2ARouter",
    "OrchestratorA2ARouter",
]


class A2AAssemblyUnavailable(RuntimeError):
    """The required governed agent/tool assembly path is not executable.

    ``reason`` is the closed code the JSON-RPC error carries in ``data.reason``
    (``ARBORA-A2A-R016.4``).
    """

    def __init__(self, message: str = "", reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class A2ACapacityRefused(RuntimeError):
    """Capacity was denied; ``code`` says whether the one re-decision is spent."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class A2AOperationBridgeUnavailable(RuntimeError):
    """``arbora.op/invoke``/``arbora.plan/confirm`` are not wired yet.

    ARBORA-A2A-R006 reuses the shared hosted-operation registry's
    ``invoke`` path (``Surface.A2A``) so a representative operation has
    identical authorization and error semantics through A2A, MCP and HTTP.
    That bridge does not exist yet, so both methods are registered on the
    A2A method table with validated params but fail closed rather than
    silently succeeding or being reported as unrecognized methods. This
    also keeps ARBORA-A2A-R005's human approval response
    (``arbora.plan/confirm``) disabled until the full signed exchange is
    implemented end to end.
    """


@runtime_checkable
class A2ARouter(Protocol):
    async def route(
        self, message: A2AMessage, *, context_budget_tokens: int | None
    ) -> A2ARouteDecision: ...


class OrchestratorA2ARouter:
    """Route an inbound message to the agent that its metadata names.

    Arbora does not rank task text against capabilities; that choice belongs
    to the engine. A message names its agent in ``metadata.agent_name``; a
    message that names none (the TUI, plain A2A clients) runs on the fleet's
    default agent (:data:`arbora.host_bindings.FLEET_AGENT_NAME`) and makes no
    decision call. With a ``decide`` port wired, a named agent is resolved by the
    engine ``A2aRoute`` decision (``ARBORA-A2A-R023``): the task dispatches only to
    the graph the sealed record names, and an abstention refuses it. The
    record's capability supplies the model profile and task class that admission
    needs (``ARBORA-A2A-R016.5``).
    """

    def __init__(
        self,
        engine_provider: Callable[[], Any],
        *,
        decide: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
        feature_schema: Callable[[str], Mapping[str, Any]] | None = None,
        tenant: Callable[[], str] | None = None,
        principal: Callable[[], str] | None = None,
        provisioning: Any = None,
        capacity: Any = None,
    ) -> None:
        self._engine_provider = engine_provider
        #: The provisioning service (ARBORA-FLEET-R005.3); ``None`` provisions nothing.
        self._provisioning = provisioning
        #: A ``CapacityLedger``; routing publishes only through acquired capacity.
        self._capacity = capacity
        self._decide = decide
        self._feature_schema = feature_schema or (lambda tenant: {})
        self._tenant = tenant or (lambda: "")
        self._principal = principal or (lambda: "")

    def acquire_capacity(self, holder: str, amount: int = 1) -> None:
        """Acquire before publish; one re-decision, then ``CAPACITY_REDECISION_EXHAUSTED``."""
        if self._capacity is None:
            return
        from arbora.control_plane.runs.capacity import CapacityReDecisionExhaustedError

        try:
            decision = self._capacity.acquire(holder, amount)
        except CapacityReDecisionExhaustedError:
            raise A2ACapacityRefused("CAPACITY_REDECISION_EXHAUSTED") from None
        if not decision.granted:
            raise A2ACapacityRefused("CAPACITY_REDECISION_OFFERED")

    def stop(self, holder: str) -> int:
        """A stopped run returns its capacity."""
        return 0 if self._capacity is None else int(self._capacity.release(holder))

    def route_work_item(self, view: Any, request: Any, *, lease_id: str) -> Any:
        """Provision through the R027 flow, keeping the work item and its single-use lease."""
        if self._provisioning is None:
            raise A2AAssemblyUnavailable("the provisioning flow is not composed")
        return self._provisioning.for_work_item(view, request, lease_id=lease_id)

    async def route(
        self, message: A2AMessage, *, context_budget_tokens: int | None
    ) -> A2ARouteDecision:
        name = str(message.metadata.get("agent_name") or "").strip()
        if not name:
            from arbora.host_bindings import FLEET_AGENT_NAME

            return A2ARouteDecision(
                agent_name=FLEET_AGENT_NAME, selection_mode="default-agent"
            )
        if self._decide is None:
            return A2ARouteDecision(agent_name=name, selection_mode="named-agent")
        return await self._a2a_route(name)

    async def _a2a_route(self, requested: str) -> A2ARouteDecision:
        from medulla.decision_client import decide_request, library, param

        from arbora.api.domain.deployment.placement import check_receipt, single_record

        assert self._decide is not None
        tenant = self._tenant()
        try:
            request = decide_request(
                tenant,
                (f"a2a.route:{requested}", "a2a_route", "ordinary"),
                # The family reads the tenant's published agent cards, not options.
                library([ROUTE_CANDIDATE_KIND]),
                self._feature_schema(tenant),
                params=[param("a2a.requested_graph_id", "text", requested)],
            )
            record = single_record(await self._decide(request))
            check_receipt(record, request, self._principal())
        except Exception:  # an unanswered route decision routes nothing
            logger.warning("a2a route failed", exc_info=True)
            raise A2AAssemblyUnavailable(
                "a2a route decision unavailable", "A2A_ROUTE_UNAVAILABLE"
            ) from None
        outcome = record.get("outcome")
        if not isinstance(outcome, Mapping):
            raise A2AAssemblyUnavailable(
                "a2a route decision unavailable", "A2A_ROUTE_UNAVAILABLE"
            )
        for code in _abstention_codes(outcome):
            if code in ROUTE_ABSTENTIONS:
                raise A2AAssemblyUnavailable("a2a route abstained", code)
        scores = outcome.get("scores") if outcome.get("outcome") == "advisory" else None
        chosen = (
            scores[0].get("option_id") if isinstance(scores, list) and scores else None
        )
        if not isinstance(chosen, str) or not chosen:
            raise A2AAssemblyUnavailable(
                "a2a route decision unavailable", "A2A_ROUTE_UNAVAILABLE"
            )
        profile, task_class, tool_refs = _route_capability(record, chosen)
        return A2ARouteDecision(
            agent_name=chosen,
            selection_mode="a2a-route",
            decision_record_ref=str(record["record_id"]),
            model_profile_ref=profile,
            task_class=task_class,
            tool_refs=tool_refs,
        )


#: The component kind (engine wire name) whose published entries are route targets.
ROUTE_CANDIDATE_KIND = "a2a_agent_card"
#: The registered refusal codes of the engine ``a2a_route`` family.
ROUTE_ABSTENTIONS = ("A2A_GRAPH_UNKNOWN", "A2A_REQUEST_MISSING")


def _abstention_codes(outcome: Mapping[str, Any]) -> list[str]:
    """Codes of ``StatisticalOutcome::Abstained``: ``reasons[].code`` of family reasons."""
    if outcome.get("outcome") != "abstained":
        return []
    reasons = outcome.get("reasons")
    return [
        str(reason["code"])
        for reason in (reasons if isinstance(reasons, list) else [])
        if isinstance(reason, Mapping)
        and reason.get("reason") == "family"
        and "code" in reason
    ]


def _route_capability(
    record: Mapping[str, Any], chosen: str
) -> tuple[str | None, str | None, tuple[str, ...]]:
    """The routed agent's model profile, task class and tool refs, when sealed.

    MEDULLA w14 (R044.2.3) seals ``record.answer = {type: "a2a_route", value:
    {graph_id, model_profile_ref?, task_class?, tool_refs[]}}``; every value is read
    from the routed graph's library entry, never from the caller. PIN-LAG: the
    pinned release seals only ``outcome.scores[0].option_id``, so all three read
    empty and a budgeted message refuses closed until the re-pin (no edit needed). ``tool_refs`` that are malformed read as absent, so the harness never
    declares a ref the engine did not seal.
    """
    answer = record.get("answer")
    value = answer.get("value") if isinstance(answer, Mapping) else None
    if (
        not isinstance(answer, Mapping)
        or answer.get("type") != "a2a_route"
        or not isinstance(value, Mapping)
        or value.get("graph_id") != chosen
    ):
        return None, None, ()
    profile, task_class = value.get("model_profile_ref"), value.get("task_class")
    return (
        profile if isinstance(profile, str) and profile else None,
        task_class if isinstance(task_class, str) and task_class else None,
        _tool_refs(value.get("tool_refs")),
    )


def _tool_refs(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > _MAX_TOOL_REFS:
        return ()
    if any(not isinstance(ref, str) or not ref.strip() for ref in raw):
        return ()
    refs = tuple(raw)
    return refs if len(set(refs)) == len(refs) else ()


_MAX_TOOL_REFS = 64
