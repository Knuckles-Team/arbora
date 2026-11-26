"""Governed agent routing for inbound A2A messages."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from .models import A2AMessage, A2ARouteDecision

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
        feature_schema: Mapping[str, Any] | None = None,
        tenant: Callable[[], str] | None = None,
        principal: Callable[[], str] | None = None,
    ) -> None:
        self._engine_provider = engine_provider
        self._decide = decide
        self._feature_schema = feature_schema or {}
        self._tenant = tenant or (lambda: "")
        self._principal = principal or (lambda: "")

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
        from medulla.decision_client import (
            DeclaredOption,
            decide_request,
            declared,
            param,
        )

        from arbora.deployment.placement import check_receipt, single_record

        assert self._decide is not None
        request = decide_request(
            self._tenant(),
            (f"a2a.route:{requested}", "a2a_route", "ordinary"),
            declared([DeclaredOption(requested)]),
            self._feature_schema,
            params=[param("a2a.requested_graph_id", "text", requested)],
        )
        try:
            record = single_record(await self._decide(request))
            check_receipt(record, request, self._principal())
        except Exception:  # noqa: BLE001 - an unanswered route decision routes nothing
            raise A2AAssemblyUnavailable(
                "a2a route decision unavailable", "A2A_ROUTE_UNAVAILABLE"
            ) from None
        for code in ("A2A_GRAPH_UNKNOWN", "A2A_REQUEST_MISSING"):
            if code in repr(record):
                raise A2AAssemblyUnavailable("a2a route abstained", code)
        outcome = record.get("outcome")
        scores = outcome.get("scores") if isinstance(outcome, Mapping) else None
        chosen = (
            scores[0].get("option_id") if isinstance(scores, list) and scores else None
        )
        if not isinstance(chosen, str) or not chosen:
            raise A2AAssemblyUnavailable(
                "a2a route decision unavailable", "A2A_ROUTE_UNAVAILABLE"
            )
        capability = outcome.get("capability") if isinstance(outcome, Mapping) else None
        capability = capability if isinstance(capability, Mapping) else {}
        return A2ARouteDecision(
            agent_name=chosen,
            selection_mode="a2a-route",
            decision_record_ref=str(record["record_id"]),
            model_profile_ref=capability.get("model_profile_ref") or None,
            task_class=capability.get("task_class") or None,
        )
