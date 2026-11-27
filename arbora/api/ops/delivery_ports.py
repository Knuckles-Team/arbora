"""The delivery ports of the production host composition (ARBORA-OPS-R896.9, R896.10, R896.12).

The delivery handlers read six ports from the served services: the lane authority, the run
starter, the identity links, the source access, the owner receipts and the code-host
connector. Each port acts as the verified caller of one request, so the host binds a
:class:`ContextPort` that builds it from that request's operation context. A port a test
binds directly is used as it is. A port whose engine shape the pinned release lacks answers
nothing, never a substitute: an assignment queues and an event is stamped with no principal.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import unavailable

SOURCE_WRITE = "source:write"
#: The engine shapes the identity-link read needs (``IdpOp::ResolveLink``, MEDULLA-IDENTITY-R752).
LINK_RESOLVE_MODELS: tuple[str, ...] = (
    "IdentityOpIdpResolveLink",
    "IdentityReplyLinkedPrincipal",
)
PATH_GRANT_MODELS: tuple[str, ...] = ("ListPathGrantsRequest", "PathGrantList")


class ContextPort:
    """A port the host builds per request, from that request's operation context."""

    def __init__(self, build: Callable[[Any], Any]) -> None:
        self._build = build

    def for_context(self, context: Any) -> Any:
        return self._build(context)


def delivery_port(context: Any, name: str) -> Any:
    """The named delivery port of this request, or ``None`` when the host binds none."""
    port = context.services.get(name)
    return port.for_context(context) if isinstance(port, ContextPort) else port


class EngineLaneAuthority:
    """The engine's development-lane methods, reached on the caller's graph.

    The sync transport is built at the first call (inside a worker thread, so it never
    blocks the serving loop). A release without the lane namespace raises
    ``DevelopmentLaneAuthorityUnavailable``, which ``start_assignment`` turns into a queue.
    """

    def __init__(self, graph: str, client_for: Callable[[str], Any]) -> None:
        self._graph = graph
        self._client_for = client_for
        self._transport: Any = None

    def _lanes(self) -> Any:
        if self._transport is None:
            from arbora.api.domain.fleet.scaling.development_lane import (
                EngineNativeDevelopmentLaneTransport,
            )

            self._transport = EngineNativeDevelopmentLaneTransport(
                self._client_for(self._graph)
            )
        return self._transport

    def status(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._lanes().status(request)

    def reserve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._lanes().reserve(request)


def _lane_authority(
    context: Any, client_for: Callable[[str], Any]
) -> EngineLaneAuthority | None:
    graph = getattr(getattr(context.caller, "session", None), "graph", None)
    if isinstance(graph, str) and graph:
        return EngineLaneAuthority(graph, client_for)
    return None


class InvokeRunStarter:
    """Starts one meristem run for an assignment through the served ``agents.run`` operation.

    The run carries the delivery context and the lane hold as received values. It starts as
    the caller of ``development.run.start``, under the key of its lane hold, so a retry of
    the same assignment replays the same run.
    """

    def __init__(self, invoke: Any, caller: Any) -> None:
        self._invoke = invoke
        self._caller = caller

    async def start(self, context: Mapping[str, Any], hold: Mapping[str, Any]) -> str:
        from arbora.security.operation_result import OpError

        message = {
            "role": "user",
            "parts": [
                {
                    "kind": "text",
                    "text": f"Deliver assignment of {context['delivery_id']}",
                }
            ],
            "metadata": {"delivery_assignment": dict(context), "lane_hold": dict(hold)},
        }
        key = f"delivery-run:{context['delivery_id']}:{hold.get('hold_id')}"
        outcome = await self._invoke(
            "agents.run", {"message": message}, self._caller, key
        )
        if isinstance(outcome, OpError):
            raise OperationRefused(outcome.code, dict(outcome.details))
        return _run_id(getattr(outcome, "value", None))


def _run_id(value: Any) -> str:
    inner = value.get("value", value) if isinstance(value, Mapping) else None
    run_id = inner.get("id") if isinstance(inner, Mapping) else None
    if not isinstance(run_id, str) or not run_id:
        raise unavailable("the run start answered no run id")
    return run_id


def _run_starter(context: Any) -> InvokeRunStarter | None:
    invoke = context.services.get("plan_step_invoke")
    return None if invoke is None else InvokeRunStarter(invoke, context.caller)


class EngineIdentityLinks:
    """A code-host actor to its arbora principal, read from the engine's identity links."""

    def __init__(self, context: Any) -> None:
        self._context = context

    async def principal_for(self, provider: str, actor: str) -> str | None:
        from arbora.api.ops.pin_guards import send_main

        try:
            answer = await send_main(
                self._context,
                "Identity",
                {
                    "op": {
                        "family": "idp",
                        "op": "resolve_link",
                        "request": {"idp_id": provider, "subject": actor},
                    }
                },
                models=LINK_RESOLVE_MODELS,
            )
        except OperationRefused as refused:
            if refused.code == "UNAVAILABLE":
                return None
            raise
        linked = answer.get("value", answer) if isinstance(answer, Mapping) else None
        principal = linked.get("principal_id") if isinstance(linked, Mapping) else None
        return principal if isinstance(principal, str) and principal else None


class EngineSourceAccess:
    """Whether a principal holds ``source:write`` on a repository, answered by the engine."""

    def __init__(self, context: Any) -> None:
        self._context = context

    async def holds(self, principal: str, scope: str, repository: str) -> bool:
        from arbora.api.ops.pin_guards import send_main

        if scope != SOURCE_WRITE:
            return False
        try:
            answer = await send_main(
                self._context,
                "ListPathGrants",
                {"request": {"principal": principal}},
                models=PATH_GRANT_MODELS,
            )
        except OperationRefused as refused:
            if refused.code == "UNAVAILABLE":
                return False
            raise
        grants = answer.get("grants") if isinstance(answer, Mapping) else None
        return any(
            isinstance(grant, Mapping)
            and grant.get("write") is True
            and grant.get("repo") == repository
            for grant in grants or ()
        )


class EngineOwnerReceipts:
    """Verifies an approval receipt of ``ARBORA-OPS-R863`` against one head commit.

    The receipt is a committed decision record that carries a reviewer's approval. It
    binds the head commit when the record's typed ``head_sha`` input is that commit.
    """

    def __init__(self, context: Any) -> None:
        self._context = context

    async def verify(self, ref: str, *, head_sha: str, tenant: str) -> str:
        from arbora.api.errors import EngineRefusal
        from arbora.api.ops._decide import committed_record

        try:
            record = await committed_record(self._context, ref)
        except EngineRefusal:
            return "missing"
        approval = record.get("approval")
        if not isinstance(approval, Mapping) or not approval.get("reviewer_principal"):
            return "missing"
        bound = _bound_head(record)
        if bound is None:
            return "missing"
        return "ok" if bound == head_sha else "head_mismatch"


def _bound_head(record: Mapping[str, Any]) -> str | None:
    inputs = record.get("inputs")
    params = inputs.get("params") if isinstance(inputs, Mapping) else None
    for item in params or ():
        if isinstance(item, Mapping) and item.get("name") == "head_sha":
            value = item.get("value")
            held = value.get("value") if isinstance(value, Mapping) else None
            return held if isinstance(held, str) else None
    return None


class FleetDeliveryConnector:
    """The code-host connector's arm tools, called through the governed fleet path."""

    def __init__(self, context: Any) -> None:
        self._context = context

    async def call(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        from arbora.api.ops.research import FleetCaller
        from arbora.identity.signed_ingress import configured_sources

        sources = configured_sources()
        provider = str(arguments.get("proposal", "")).split(":", 1)[0]
        source = sources.get(provider)
        if source is None and len(sources) == 1:
            source = next(iter(sources.values()))
        if source is None or not source.server:
            raise unavailable("no code-host connector is configured for the proposal")
        return await FleetCaller(self._context).call(
            source.server, f"{source.id}_{tool}", dict(arguments)
        )


def delivery_bindings(lane_client: Callable[[str], Any]) -> dict[str, ContextPort]:
    """The six delivery ports of the production host composition.

    ``lane_client`` is the shared synchronous engine client of a graph, from the host.
    """
    return {
        "delivery_lanes": ContextPort(
            lambda context: _lane_authority(context, lane_client)
        ),
        "delivery_run_starter": ContextPort(_run_starter),
        "identity_links": ContextPort(EngineIdentityLinks),
        "source_access": ContextPort(EngineSourceAccess),
        "owner_receipts": ContextPort(EngineOwnerReceipts),
        "delivery_connector": ContextPort(FleetDeliveryConnector),
    }


__all__ = [
    "ContextPort",
    "EngineIdentityLinks",
    "EngineLaneAuthority",
    "EngineOwnerReceipts",
    "EngineSourceAccess",
    "FleetDeliveryConnector",
    "InvokeRunStarter",
    "delivery_bindings",
    "delivery_port",
]
