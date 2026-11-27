"""The served process's one EG client pool and its session-routed engine view.

The ``arbora`` process reaches medulla through exactly one
:class:`~arbora.epistemic.EpistemicClientPool`, built from
``GRAPH_SERVICE_ENDPOINTS``. The operation-invoke API
(:mod:`arbora.mcp_server.served_invocation`) and the serving lifecycle's
startup composition (base graphs, catalog authorities, fleet onboarding,
decide consumers, co-services) share it.

Startup code addresses the engine as ``engine.graph_compute.for_graph(graph)
.async_client``. That client is a *view*: every request it sends resolves the
ambient verified :class:`~arbora.security.session.GraphSession` at call
time and binds that session's engine claims to the pooled transport for that
one request. The view never caches or widens authority, and it holds no graph
behavior of its own; EG owns every fact and every method.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any, cast

from arbora.core.environment import read_env
from arbora.core.settings import RuntimeConfig
from arbora.epistemic import run_blocking
from arbora.security.anonymous import client_scope, session_context
from arbora.security.session import SessionRouted
from arbora.unique_records import record_body

__all__ = [
    "EngineHandles",
    "ServedEngine",
    "SessionRoutedClient",
    "engine_unbound",
    "no_process_session",
]


def no_process_session() -> None:
    """The process-session provider of a host that binds none."""
    return None


def _session_context(session: Any) -> Any:
    """The pool's :class:`ClientContext` for one verified session."""
    from arbora.epistemic import ClientContext

    claims: Mapping[str, Any] = session_context(session)
    return ClientContext(
        auth_mode=str(claims.get("auth_mode", "authenticated")),
        principal=str(claims["principal"]),
        tenant=str(claims["tenant"]),
        audience=str(claims["audience"]),
        agent_id=str(claims["agent_id"]),
        roles=tuple(claims.get("roles") or ()),
        scopes=tuple(claims.get("scopes") or ()),
        policy_version=str(claims["policy_version"]),
        delegation=tuple(claims.get("delegation") or ()),
        priority=claims.get("priority"),
    )


def _ambient_session(process_session: Callable[[], Any] = no_process_session) -> Any:
    """The request's verified session, else the stdio process session."""
    from arbora.security.session import SessionRequiredError, current_session

    session = current_session() or process_session()
    if session is None:
        raise SessionRequiredError("a verified GraphSession is required")
    return session


class SessionRoutedClient(SessionRouted):
    """An EG client view bound to one graph, authorized per request.

    The generated ``send_*`` functions call ``client._send``; the typed
    sub-clients (``client.control_leases.acquire(...)``) are reached by
    attribute. Both resolve the pooled transport and the ambient session's
    claims when the request is sent.
    """

    def __init__(
        self,
        pool: Any,
        graph: str,
        process_session: Callable[[], Any] = no_process_session,
    ) -> None:
        self._pool = pool
        self._graph = graph
        self._process_session = process_session

    @property
    def graph_name(self) -> str:
        return self._graph

    async def _bound(self) -> tuple[Any, Any]:
        context = _session_context(_ambient_session(self._process_session))
        client = await self._pool.connected_client(context, self._graph)
        return client, context

    async def _send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        graph: str | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        client, context = await self._bound()
        with client_scope(client, context.to_claims()):
            return await client._send(
                method,
                params,
                graph or self._graph,
                idempotency_key=idempotency_key,
            )

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _RoutedAttribute(self, (name,))


class _RoutedAttribute:
    """A deferred attribute path on the pooled client, resolved per call."""

    def __init__(self, view: SessionRoutedClient, path: tuple[str, ...]) -> None:
        self._view = view
        self._path = path

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _RoutedAttribute(self._view, (*self._path, name))

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        client, context = await self._view._bound()
        target: Any = client
        for part in self._path:
            target = getattr(target, part)
        with client_scope(client, context.to_claims()):
            return await target(*args, **kwargs)


class _GraphView:
    def __init__(
        self, pool: Any, graph: str, process_session: Callable[[], Any]
    ) -> None:
        self.graph_name = graph
        self.async_client = SessionRoutedClient(pool, graph, process_session)


class _GraphCompute:
    """``for_graph(graph).async_client``: the shape startup composition reads."""

    def __init__(
        self, pool: Any, process_session: Callable[[], Any] = no_process_session
    ) -> None:
        self._pool = pool
        self._process_session = process_session

    def for_graph(self, graph: str) -> _GraphView:
        return _GraphView(self._pool, str(graph), self._process_session)


_PARAMETER = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def _cypher_literal(value: Any) -> str:
    """One Cypher literal for a bound parameter value (EG takes no parameters)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_cypher_literal(item) for item in value) + "]"
    raise TypeError(f"unsupported Cypher parameter type {type(value).__name__}")


def _bind_parameters(query: str, params: Mapping[str, Any] | None) -> str:
    values = dict(params or {})

    def literal(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise KeyError(f"Cypher parameter ${name} is not bound")
        return _cypher_literal(values[name])

    return _PARAMETER.sub(literal, query)


class _BlockingWorkItems:
    """EG's ``work_items`` methods, called synchronously under the ambient session.

    :mod:`arbora.work_items` (A2A tasks, browser control, messaging leases)
    calls ``engine.work_items.<verb>(...)`` from synchronous code; each verb
    runs the async EG method to completion through :func:`run_blocking`.
    """

    def __init__(self, routed: Any) -> None:
        self._routed = routed

    def get_run(self, *, tenant: str, run_id: str) -> dict[str, Any] | None:
        """Read immutable run admission through the caller-routed EG client."""
        from arbora.native_run_admission import require_lookup

        method = require_lookup(self._routed)
        return run_blocking(lambda: method(tenant=tenant, run_id=run_id))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        method = getattr(self._routed, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            return run_blocking(lambda: method(*args, **kwargs))

        return call


class _BlockingControlLeases:
    """EG's ``control_leases`` methods, called synchronously under one caller.

    The action policy and the fleet approval routes are synchronous; each
    verb runs the async EG method to completion through :func:`run_blocking`.
    ``tenant`` is the verified session's tenant, which every control-lease
    request must carry (ARBORA-IDENTITY-R019.8).
    """

    def __init__(self, routed: Any, tenant: str) -> None:
        self._routed = routed
        self.tenant = tenant

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        method = getattr(self._routed, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            return run_blocking(lambda: method(*args, **kwargs))

        return call


class ServedEngine:
    """The served process's engine handle over the one EG client pool."""

    def __init__(
        self, pool: Any, process_session: Callable[[], Any] = no_process_session
    ) -> None:
        self.pool = pool
        self.process_session = process_session
        self.graph_compute = _GraphCompute(pool, process_session)

    @property
    def browser_control_state(self) -> Any:
        """Browser records through public EG methods under the current caller."""
        from arbora.mcp_server.browser_state import ServedBrowserState

        return ServedBrowserState(self.pool)

    def batch_typed_mutations(
        self, mutations: list[dict[str, Any]], *, session: Any
    ) -> bool:
        """Atomically persist a typed browser trace under its exact caller."""
        return self.browser_control_state.batch_typed_mutations(
            mutations, session=session
        )

    @property
    def work_item_graph(self) -> str:
        """The native graph used by this engine's WorkItem namespace."""
        from arbora.semantic_provisioning import CONTROL_GRAPH

        return CONTROL_GRAPH

    @property
    def work_items(self) -> _BlockingWorkItems:
        """EG WorkItems, held on the tenant's control graph (``__control__``)."""
        return _BlockingWorkItems(
            self.graph_compute.for_graph(self.work_item_graph).async_client.work_items
        )

    @property
    def control_leases(self) -> _BlockingControlLeases:
        """EG control leases on the session graph, under the caller's tenant."""
        session = _ambient_session(self.process_session)
        return _BlockingControlLeases(
            self.graph_compute.for_graph(
                str(session.graph)
            ).async_client.control_leases,
            str(session.tenant),
        )

    async def health(self, graph: str) -> Mapping[str, Any]:
        """EG's own health document, read under the ambient session."""
        return await self.graph_compute.for_graph(graph).async_client.health()

    def _session_client(self) -> SessionRoutedClient:
        return self.graph_compute.for_graph(
            str(_ambient_session(self.process_session).graph)
        ).async_client

    def query_cypher(
        self, query: str, params: Mapping[str, Any] | None = None
    ) -> list[Any]:
        """Run one read-only Cypher query on the session graph through EG."""
        client = self._session_client()
        bound = _bind_parameters(query, params)

        async def run() -> Any:
            # The public client owns decoding columns plus MessagePack row
            # blobs. Returning raw generated payload rows loses their names.
            return await client.query.cypher_read(bound)

        return list(run_blocking(run))

    async def query_cypher_async(
        self,
        query: str,
        params: Mapping[str, Any] | None = None,
        *,
        session: Any = None,
    ) -> list[Any]:
        """Read through the public client on this loop under one exact caller."""
        from arbora.security.session import resolve_session

        caller = resolve_session(session, required_scope="kg:read")
        bound = _bind_parameters(query, params)
        async with self.pool.bind(
            _session_context(caller), caller.graph, required_scope="kg:read"
        ) as client:
            resolve_session(caller, required_scope="kg:read")
            result = await client.query.cypher_read(bound)
            resolve_session(caller, required_scope="kg:read")
            return list(result)

    def add_node(
        self,
        node_id: str,
        label: str,
        properties: Mapping[str, Any] | None = None,
    ) -> str:
        """Write one labelled node on the session graph through EG ``AddNode``."""
        import msgpack
        from medulla.generated.graph import send_add_node

        client = self._session_client()
        body = record_body(label, properties)
        params = {
            "node_id": node_id,
            "properties_msgpack": msgpack.packb(body, use_bin_type=True),
        }

        async def run() -> Any:
            return await send_add_node(
                client,
                params,
                client.graph_name,
                idempotency_key=f"arbora:add-node:{node_id}",
            )

        return str(run_blocking(run))

    def create_node(
        self,
        node_id: str,
        label: str,
        properties: Mapping[str, Any] | None = None,
    ) -> bool:
        """Create one labelled node through EG ``CreateNodeIfAbsent``.

        An append-only record with a unique id (a ``RunEvent``) is a create,
        not an upsert. EG 2.27.2 refuses ``AddNode`` on a graph whose write
        path is cold (``INTERNAL: unclassified engine refusal``) while
        ``CreateNodeIfAbsent`` on the same graph is admitted. Returns whether
        this call created the node.
        """
        import msgpack
        from medulla.generated.graph import send_create_node_if_absent

        client = self._session_client()
        body = record_body(label, properties)
        params = {
            "node_id": node_id,
            "properties_msgpack": msgpack.packb(body, use_bin_type=True),
        }

        async def run() -> Any:
            return await send_create_node_if_absent(
                client,
                params,
                client.graph_name,
                idempotency_key=f"arbora:create-node:{node_id}",
            )

        return bool(run_blocking(run))

    def compare_and_set_node(
        self,
        node_id: str,
        conditions: Mapping[str, Any],
        updates: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> bool:
        """Merge ``updates`` into one node iff every condition holds (EG CAS).

        EG ``CompareAndSetNodeFields`` reads a missing field as ``None``, keeps
        every untouched property and re-files the node's labels when a label
        field changes. Returns whether the merge applied.
        """
        import msgpack
        from medulla.generated.graph import send_compare_and_set_node_fields

        client = self._session_client()
        params = {
            "node_id": node_id,
            "conditions_msgpack": msgpack.packb(dict(conditions), use_bin_type=True),
            "updates_msgpack": msgpack.packb(dict(updates), use_bin_type=True),
        }

        async def run() -> Any:
            return await send_compare_and_set_node_fields(
                client,
                params,
                client.graph_name,
                idempotency_key=idempotency_key,
            )

        return bool(run_blocking(run))


class EngineAttachments:
    """The local engines one host is attached to, by endpoint address, and its spawn count.

    The ``attachments`` of the host's engine handles; the lock guards both.
    """

    def __init__(self) -> None:
        self.attached: dict[str, Any] = {}
        self.lock = threading.Lock()
        self.spawned = 0


def _build_process_pool(settings: RuntimeConfig, attachments: EngineAttachments) -> Any:
    """Build the one EG client pool of a host from its configured endpoint.

    With ``GRAPH_SERVICE_ENDPOINTS`` unset, the host attaches to the local
    loopback engine and starts one only when none answers
    (``ARBORA-DEPLOY-R018.9``). A configured endpoint never starts an engine.
    """
    from medulla import MedullaClient

    from arbora.epistemic import ConnectClient, EpistemicClientPool
    from arbora.mcp_server.composition import (
        attach_reused_engine,
        ensure_local_engine,
        local_engine_config,
        parse_engine_endpoint,
        require_remote_engine_credential,
    )

    endpoints = settings.graph_service_endpoints
    if endpoints:
        # A configured endpoint is connect-only: nothing starts locally.
        endpoint = parse_engine_endpoint(
            next((item for item in endpoints if str(item).strip()), "")
        )
        require_remote_engine_credential(endpoint)
        if str(read_env("ENGINE_REUSE")).strip().lower() in {"1", "true"}:
            attach_reused_engine(endpoint)
    else:
        endpoint = ensure_local_engine(local_engine_config(settings), attachments)
    return EpistemicClientPool(
        cast(ConnectClient, MedullaClient.connect),
        **endpoint.client_kwargs(),
    )


def engine_unbound() -> Any:
    """The engine provider of a server composed without an engine."""
    raise RuntimeError("this server was composed without an engine")


def _pool_factory_over(
    pool_factory: Callable[[], Any] | None,
    config: RuntimeConfig,
    attachments: EngineAttachments,
) -> Callable[[], Any]:
    """The given pool factory, or the process pool over the host's config."""
    if pool_factory is not None:
        return pool_factory
    return partial(_build_process_pool, config, attachments)


class EngineHandles:
    """One host's EG client pool and its engine handle (ARBORA-HOST-R039.8).

    The ``engine`` field of the host context. The pool is built on first use and
    the handle is rebuilt only when the pool changes; both live under this
    object's own lock, so two hosts hold two separate pools.
    """

    __slots__ = (
        "_engine",
        "_lock",
        "_pool",
        "_pool_factory",
        "_process_session",
        "attachments",
    )

    def __init__(
        self,
        pool_factory: Callable[[], Any] | None = None,
        process_session: Callable[[], Any] = no_process_session,
        *,
        config: RuntimeConfig,
    ) -> None:
        self._lock = threading.Lock()
        self._process_session = process_session
        self._pool: Any = None
        self._engine: ServedEngine | None = None
        #: The local engines this host started or attached to.
        self.attachments = EngineAttachments()
        self._pool_factory = _pool_factory_over(pool_factory, config, self.attachments)

    def pool(self) -> Any:
        """This host's one EG client pool, built on first use."""
        with self._lock:
            if self._pool is None:
                self._pool = self._pool_factory()
            return self._pool

    def served(self) -> ServedEngine:
        """This host's engine handle over :meth:`pool`."""
        pool = self.pool()
        with self._lock:
            if self._engine is None or self._engine.pool is not pool:
                self._engine = ServedEngine(pool, self._process_session)
            return self._engine

    def reset(self) -> None:
        """Forget the pool and the handle (process teardown only)."""
        with self._lock:
            self._pool = None
            self._engine = None
