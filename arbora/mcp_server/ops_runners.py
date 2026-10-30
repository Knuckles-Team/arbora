"""Engine-backed runners the served bootstrap binds for ``decide.*`` and ``work.*``.

ARBORA-OPS-R021.1 / R022.1 declare ``decide_runner`` and ``work_runner`` as
service-bound ports.  Medulla owns the decision and the WorkItem records; these
runners only send the pinned engine methods (``DecisionCommit``,
``ListWorkItems``, ``GetWorkItem``) under Arbora's verified process identity,
one tenant per call, and keep no state of their own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused

__all__ = ["EngineDecideRunner", "EngineWorkRunner"]

ClientFactory = Callable[[str], Awaitable[Any]]


async def _send(
    client: Any, method: str, params: dict[str, Any], graph: str, key: str | None
) -> Any:
    from epistemic_graph.generated import send_by_id

    return await send_by_id(client, method, params, graph, idempotency_key=key)


class EngineDecideRunner:
    """``decide_runner``: commit one decision record through ``DecisionCommit``."""

    def __init__(self, client: ClientFactory, *, graph: str) -> None:
        self._client = client
        self._graph = graph

    async def commit(
        self, request: Mapping[str, Any], *, tenant: str
    ) -> Mapping[str, Any]:
        client = await self._client(tenant)
        result = await _send(
            client, "DecisionCommit", {"request": dict(request)}, self._graph, None
        )
        return dict(result) if isinstance(result, Mapping) else {"result": result}


class EngineWorkRunner:
    """``work_runner``: tenant-scoped WorkItem reads on the control graph."""

    def __init__(self, client: ClientFactory, *, graph: str) -> None:
        self._client = client
        self._graph = graph

    async def list_items(
        self, *, tenant: str, principal: str, cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        client = await self._client(tenant)
        params: dict[str, Any] = {"tenant": tenant, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        result = await _send(client, "ListWorkItems", params, self._graph, None)
        return dict(result)

    async def get_item(
        self, item_id: str, *, tenant: str, principal: str
    ) -> Mapping[str, Any] | None:
        client = await self._client(tenant)
        result = await _send(
            client,
            "GetWorkItem",
            {"tenant": tenant, "work_item_id": item_id},
            self._graph,
            None,
        )
        return dict(result) if result else None

    async def create_offer(
        self,
        item_id: str,
        *,
        terms: Mapping[str, Any],
        tenant: str,
        principal: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        # Offers are priced on a Gap (WorkOfferPut), not on a bare WorkItem id;
        # the item-to-gap mapping has no engine owner yet, so fail closed.
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "work offer pricing has no engine mapping"}
        )
