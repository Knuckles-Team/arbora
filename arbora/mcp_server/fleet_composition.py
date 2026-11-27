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

from arbora.api.mcp.discovery import not_ready_detail

__all__ = ["LateFleetGateway", "bind_fleet_gateway", "tool_ranker_for"]


def _unavailable() -> Exception:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused("UNAVAILABLE", not_ready_detail())


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
    from arbora.security.session import use_session

    with use_session(session):
        yield


class LateFleetGateway:
    """The served fleet gateway, bound once the multiplexer is attached."""

    def __init__(self) -> None:
        self._gateway: Any = None
        self._reader: Any = None
        self.ops: Any = None

    def bind(self, gateway: Any, ops: Any, reader: Any = None) -> None:
        if self._gateway is not None:
            raise RuntimeError("the fleet gateway is already bound")
        self._gateway = gateway
        self.ops = ops
        self._reader = reader

    async def read_catalog(self, caller: Any) -> Any:
        """The verified fleet catalog snapshot for the caller (ARBORA-FLEET-R052.2)."""
        self._bound()
        if self._reader is None:
            raise _unavailable()
        with _caller_session(caller):
            return await self._reader.read(kinds=("mcp_server",))

    async def generation_status(self, caller: Any) -> Any:
        """This replica's catalog generation and convergence state (ARBORA-FLEET-R051.2.1)."""
        self._bound()
        if self._reader is None:
            raise _unavailable()
        with _caller_session(caller):
            return await self._reader.generation_status()

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

    async def pin(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().pin(caller, **params)

    async def unpin(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().unpin(caller, **params)

    async def catalog_refresh(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().catalog_refresh(caller, **params)

    async def pins_list(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().pins_list(caller, **params)

    async def prompts_list(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().prompts_list(caller, **params)

    async def prompts_get(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().prompts_get(caller, **params)

    async def resources_list(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().resources_list(caller, **params)

    async def resources_read(self, caller: Any, **params: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().resources_read(caller, **params)

    async def effect(self, op: Any, params: Mapping[str, Any], caller: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().effect(op, params, caller)

    async def call(self, caller: Any, *args: Any, **kwargs: Any) -> Any:
        with _caller_session(caller):
            return await self._bound().call(caller, *args, **kwargs)

    @property
    def ready(self) -> bool:
        """Whether the multiplexer is attached and the gateway bound."""
        return self._gateway is not None

    async def fleet_catalog(
        self, *, caller: Any, server: str = "", include_tools: bool = True
    ) -> Mapping[str, Any]:
        """``find(op="catalog")``: the multiplexer's one catalog handler (R063)."""
        with _caller_session(caller):
            return await self._bound().catalog(
                caller, server=server, include_tools=include_tools
            )

    async def fleet_search(
        self,
        *,
        caller: Any,
        query: str,
        context_budget_tokens: int | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """The intent surface's fleet discovery: the caller's authorized tools."""
        if "mcp:discover" not in caller.effective_scopes:
            return ()
        if self._gateway is None:
            raise _unavailable()
        params: dict[str, Any] = {"query": query, "kinds": ("tool",)}
        if context_budget_tokens is not None:
            params["context_budget_tokens"] = context_budget_tokens
        found = await self._gateway.search(caller, **params)
        return tuple(found.get("items", ()))


async def _scoped(item: Any, caller: Any) -> bool:
    """Admission for one catalog item: the caller holds its required scopes."""
    scopes: frozenset[str] = getattr(caller, "effective_scopes", frozenset())
    return bool(item.required_scopes <= scopes)


def session_key(caller: Any) -> str | None:
    principal = getattr(caller, "principal", None)
    tenant = getattr(caller, "tenant", None)
    if not principal or not tenant:
        return None
    return f"{tenant}:{principal}"


def _pin_store(services: Any) -> Any:
    """The engine-backed pin store over the plan store's lease wiring.

    The served invocation composes one service-authorized control-lease
    client for ``EgPlanStore``; the pin store reuses that exact wiring and
    adds no second database (ARBORA-FLEET-R059.3). Without it the pin
    operations refuse ``UNAVAILABLE`` (fail closed)."""
    plans = getattr(services, "plans", None)
    # ``EgPlanStore._leases`` already is the client's ``control_leases``; the
    # served one is a per-call proxy that answers any attribute, so it is
    # handed over as ``control_leases`` rather than probed for the name.
    leases = getattr(plans, "_leases", None)
    if leases is None:
        return None
    from types import SimpleNamespace

    from arbora.fleet.pin_store import EgPinStore

    return EgPinStore(SimpleNamespace(control_leases=leases))


def tool_ranker_for(
    engine_send: Any, feature_schema: Any = None, principal: Any = None
) -> Any:
    """The engine tool ranker of the host, or ``None`` (ARBORA-OPS-R866).

    Tool search orders by the engine ranking once the pinned contract names the
    ``tool_relevance`` question; without a send, the tenant feature-schema pin, the
    service principal or the question the catalog order stands (the inactive gate is
    reported by ``arbora.pin_lag.gates``).
    """
    from arbora.fleet.tool_ranking import engine_tool_order
    from arbora.pin_lag.medulla import question_kind_served

    if None in (engine_send, feature_schema, principal):
        return None
    if not question_kind_served("tool_relevance"):
        return None
    return engine_tool_order(
        engine_send, feature_schema=feature_schema, principal=principal
    )


def bind_fleet_gateway(
    late: LateFleetGateway,
    *,
    mux: Any,
    reader: Any,
    services: Any,
    engine_send: Any = None,
    feature_schema: Any = None,
    principal: Any = None,
) -> Any:
    """Compose the multiplexer operation service and governed gateway; bind both.

    A loaded connector tool is mounted natively on the host MCP server and
    shown only to the sessions that loaded it, by the governed visibility
    middleware ``register_mcp_tools`` installs (ARBORA-OPS-R035.2).
    """
    from arbora.api.invoke.pipeline import invoke
    from arbora.fleet.catalog_composition import compose_multiplexer_ops
    from arbora.fleet.feeds import FEED_INTAKE
    from arbora.fleet.gateway_ops import compose_fleet_gateway
    from arbora.fleet.multiplexer import clean_tool_name, make_governed_tool_mount
    from arbora.mcp_server.registration import RESIDENT_NAMES

    def native_name(item: Any) -> str:
        return clean_tool_name(mux.server_prefix(item.server), item.server, item.name)

    # The governed mount adds the forwarder to the host MCP server; the
    # session-visibility middleware of ``register_mcp_tools`` hides it from
    # every session that has not loaded it.
    mount = make_governed_tool_mount(mux._host_mcp, mux, native_name, RESIDENT_NAMES)

    async def notify(_session_key: str) -> bool:
        return await mux.notify_pending_tools_changed()

    async def bound_invoke(op: str, params: Any, caller: Any, surface: Any) -> Any:
        return await invoke(op, params, caller, surface, services=services)

    ops = compose_multiplexer_ops(
        reader=reader,
        mux=mux,
        sdk_entries=FEED_INTAKE.entries,
        visible=_scoped,
        loadable=_scoped,
        callable_item=_scoped,
        mount=mount,
        notify=notify,
        native_name=native_name,
        invoke=bound_invoke,
        session_key_for=session_key,
        pin_store=_pin_store(services),
        tool_ranker=tool_ranker_for(engine_send, feature_schema, principal),
    )

    async def policy_check(server: str, tool: str, caller: Any) -> bool:
        item = await ops.admitted_tool(caller, server, tool)
        return await _scoped(item, caller)

    gateway = compose_fleet_gateway(ops=ops, mux=mux, policy_check=policy_check)
    late.bind(gateway, ops, reader)
    return gateway
