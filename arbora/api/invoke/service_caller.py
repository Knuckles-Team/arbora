"""The per-tenant service caller: the one way the host calls as itself (ARBORA-OPS-R958).

A background service (the curator scheduler) has no request and no bearer. It
calls through the host's own verified process authority, for one tenant, by one
of two doors: an engine method under exact allowlisted service scopes
(:meth:`ServiceCallers.send`, through ``BoundOperationRuntime.as_service``), or a
served operation through the invoke pipeline with a caller projected from the
process session (:meth:`ServiceCallers.invoke`). Arbora serves the process
tenant's graph only, so any other tenant is refused exactly like an absent one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import send_on_client

#: ``(op_id, params, caller, idempotency_key) -> result``: the served pipeline.
InvokeOp = Callable[[str, Mapping[str, Any], Any, str], Awaitable[Any]]

SERVICE_CREDENTIAL_KIND = "service"


class ServiceCallers:
    """Mint service calls for a tenant from the process authority, nothing wider."""

    def __init__(
        self,
        *,
        process_session: Callable[[], Any],
        runtime: Any,
        graph: str,
        invoke: InvokeOp,
    ) -> None:
        self._session = process_session
        self._runtime = runtime
        self._graph = graph
        self._invoke = invoke

    @property
    def graph(self) -> str:
        """The graph every service call is dispatched on."""
        return self._graph

    async def tenants(self) -> Sequence[str]:
        """The tenants this process serves: the process tenant only."""
        return (str(self._session().tenant),)

    def caller(self, tenant: str, *, request_id: str = "") -> Any:
        """The process authority as a request caller for ``tenant``."""
        from arbora.api.mcp.caller import caller_from_session

        session = self._session()
        if str(session.tenant) != tenant:
            raise OperationRefused("NOT_FOUND")
        return caller_from_session(
            session,
            credential_kind=SERVICE_CREDENTIAL_KIND,
            request_id=request_id,
        )

    async def send(
        self,
        tenant: str,
        method: str,
        params: Mapping[str, Any],
        *,
        scopes: frozenset[str],
        key: str | None = None,
    ) -> Any:
        """One engine method as the service principal under exactly ``scopes``."""
        if str(self._session().tenant) != tenant:
            raise OperationRefused("NOT_FOUND")
        async with self._runtime.as_service(tenant, required_scopes=scopes) as client:
            return await send_on_client(
                client, method, dict(params), self._graph, key=key
            )

    async def invoke(
        self, tenant: str, op_id: str, params: Mapping[str, Any], key: str
    ) -> Any:
        """One served operation as the service principal for ``tenant``."""
        return await self._invoke(op_id, params, self.caller(tenant), key)
