"""Compose the governed fleet gateway into the served process.

The operation-invoke services are built before the MCP server and its fleet
multiplexer exist, so they carry a :class:`LateFleetGateway`: the
``fleet_gateway`` service and ``fleet_effect`` port the registry's ``fleet.*``
operations call. Once the multiplexer is attached, :func:`bind_fleet_gateway`
composes the multiplexer's operation service and the governed
:class:`~arbora.fleet.gateway_ops.FleetGateway` over it, and binds them.

Every fleet discovery and call then runs through ``invoke`` (arbora
admission: scopes, policy, audit) and the gateway's own per-call recheck of
the admitted catalog item. Until binding, every port refuses ``UNAVAILABLE``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

__all__ = ["LateFleetGateway", "bind_fleet_gateway"]


def _unavailable() -> Exception:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused("UNAVAILABLE", {"reason": "fleet gateway is not bound"})


@contextmanager
def _caller_session(caller: Any) -> Iterator[None]:
    """Bind the verified caller's own session for one fleet read or call.

    The fleet catalog reads EG through the ambient session (re-targeted to
    ``__commons__`` and the tenant graph). The MCP surface binds one per
    request; the HTTP operation API does not, so without this a catalog read
    or ``fleet.call`` from ``/api/v1/ops`` raised ``SessionRequiredError``
    (``INTERNAL``/``UNAVAILABLE``).
    """
    session = getattr(caller, "session", None)
    if session is None:
        yield
        return
    from arbora.api.contracts.session import use_session

    with use_session(session):
        yield


class LateFleetGateway:
    """The served fleet gateway, bound once the multiplexer is attached."""

    def __init__(self) -> None:
        self._gateway: Any = None
        self.ops: Any = None

    def bind(self, gateway: Any, ops: Any) -> None:
        if self._gateway is not None:
            raise RuntimeError("the fleet gateway is already bound")
        self._gateway = gateway
        self.ops = ops

    def _bound(self) -> Any:
        if self._gateway is None:
            raise _unavailable()
        return self._gateway

    async def search(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().search(caller, **params)

    async def list(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().list(caller, **params)

    async def status(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().status(caller, **params)

    async def load(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().load(caller, **params)

    async def unload(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().unload(caller, **params)

    async def effect(self, op: Any, params: Mapping[str, Any], caller: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().effect(op, params, caller)

    async def call(self, caller: Any, *args: Any, **kwargs: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().call(caller, *args, **kwargs)

    async def fleet_search(
        self,
        *,
        caller: Any,
        query: str,
        context_budget_tokens: int | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """The intent surface's fleet discovery: the caller's authorized tools."""
        if self._gateway is None or "mcp:discover" not in caller.effective_scopes:
            return ()
        params: dict[str, Any] = {"query": query, "kinds": ("tool",)}
        if context_budget_tokens is not None:
            params["context_budget_tokens"] = context_budget_tokens
        found = await self._gateway.search(caller, **params)
        return tuple(found.get("items", ()))


async def _scoped(item: Any, caller: Any) -> bool:
    """Admission for one catalog item: the caller holds its required scopes."""
    scopes: frozenset[str] = getattr(caller, "effective_scopes", frozenset())
    return bool(item.required_scopes <= scopes)


def _session_key(caller: Any) -> str | None:
    principal = getattr(caller, "principal", None)
    tenant = getattr(caller, "tenant", None)
    if not principal or not tenant:
        return None
    return f"{tenant}:{principal}"


def bind_fleet_gateway(
    late: LateFleetGateway,
    *,
    mux: Any,
    reader: Any,
    services: Any,
) -> Any:
    """Compose the multiplexer operation service and governed gateway; bind both.

    Native per-session tool mounts need the governed MCP visibility cutover
    (``arbora.api.mcp.registration``); until it is served, a connector tool
    is called through ``act(op='fleet.call')``, which mounts the child on
    demand. Load requests therefore refuse instead of mounting a tool every
    session would see.
    """
    from arbora.api.invoke.pipeline import invoke
    from arbora.fleet.catalog_composition import compose_multiplexer_ops
    from arbora.fleet.gateway_ops import compose_fleet_gateway

    async def no_sdk_entries() -> tuple[Mapping[str, Any], ...]:
        return ()

    async def refuse_mount(_item: Any, _forwarder: Any) -> None:
        raise PermissionError(
            "native fleet mounts need the governed MCP visibility surface; "
            "call the tool with act(op='fleet.call')"
        )

    async def no_notify(_session_key: str) -> bool:
        return False

    async def bound_invoke(op: str, params: Any, caller: Any, surface: Any) -> Any:
        return await invoke(op, params, caller, surface, services=services)

    ops = compose_multiplexer_ops(
        reader=reader,
        mux=mux,
        sdk_entries=no_sdk_entries,
        visible=_scoped,
        loadable=_scoped,
        callable_item=_scoped,
        mount=refuse_mount,
        notify=no_notify,
        invoke=bound_invoke,
        session_key_for=_session_key,
    )

    async def policy_check(server: str, tool: str, caller: Any) -> bool:
        item = await ops.admitted_tool(caller, server, tool)
        return await _scoped(item, caller)

    gateway = compose_fleet_gateway(ops=ops, mux=mux, policy_check=policy_check)
    late.bind(gateway, ops)
    return gateway
