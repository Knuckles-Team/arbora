"""The served process's one EG client pool and its session-routed engine view.

The ``arbora`` process reaches epistemic-graph through exactly one
:class:`~arbora.epistemic.EpistemicClientPool`, built from
``GRAPH_SERVICE_ENDPOINTS``. The operation-invoke API
(:mod:`arbora.mcp_server.served_invocation`) and the serving lifecycle's
startup composition (base graphs, catalog authorities, fleet onboarding,
decide consumers, co-services) share it.

Startup code addresses the engine as ``engine.graph_compute.for_graph(graph)
.async_client``. That client is a *view*: every request it sends resolves the
ambient verified :class:`~arbora.api.contracts.session.GraphSession` at call
time and binds that session's engine claims to the pooled transport for that
one request. The view never caches or widens authority, and it holds no graph
behavior of its own; EG owns every fact and every method.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import re
import threading
from collections.abc import Mapping
from typing import Any, cast

from arbora.security.anonymous import client_scope, session_context
from arbora.unique_records import record_body

__all__ = [
    "ServedEngine",
    "SessionRoutedClient",
    "process_engine_pool",
    "reset_process_engine",
    "served_engine",
]

_LOCK = threading.Lock()
_POOL: Any = None
_ENGINE: ServedEngine | None = None


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


def _ambient_session() -> Any:
    """The request's verified session, else the stdio process session."""
    from arbora.api.contracts.session import SessionRequiredError, current_session
    from arbora.mcp_server.bootstrap import _process_session

    session = current_session() or _process_session()
    if session is None:
        raise SessionRequiredError("a verified GraphSession is required")
    return session


class SessionRoutedClient:
    """An EG client view bound to one graph, authorized per request.

    The generated ``send_*`` functions call ``client._send``; the typed
    sub-clients (``client.control_leases.acquire(...)``) are reached by
    attribute. Both resolve the pooled transport and the ambient session's
    claims when the request is sent.
    """

    def __init__(self, pool: Any, graph: str) -> None:
        self._pool = pool
        self._graph = graph

    @property
    def graph_name(self) -> str:
        return self._graph

    async def _bound(self) -> tuple[Any, Any]:
        context = _session_context(_ambient_session())
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
    def __init__(self, pool: Any, graph: str) -> None:
        self.graph_name = graph
        self.async_client = SessionRoutedClient(pool, graph)


class _GraphCompute:
    """``for_graph(graph).async_client``: the shape startup composition reads."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def for_graph(self, graph: str) -> _GraphView:
        return _GraphView(self._pool, str(graph))


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


def _run_blocking(coroutine_factory: Any) -> Any:
    """Run one EG request to completion from synchronous code.

    Synchronous callers (the action-policy ledger) may run inside an event
    loop; the request then runs on a worker thread with its own loop, under a
    copy of the caller's context so the ambient session still authorizes it.
    """
    from arbora.epistemic import temporary_client_scope

    async def request() -> Any:
        async with temporary_client_scope():
            return await coroutine_factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(request())
    context = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(context.run, asyncio.run, request()).result()


class _BlockingWorkItems:
    """EG's ``work_items`` methods, called synchronously under the ambient session.

    :mod:`arbora.work_items` (A2A tasks, browser control, messaging leases)
    calls ``engine.work_items.<verb>(...)`` from synchronous code; each verb
    runs the async EG method to completion through :func:`_run_blocking`.
    """

    def __init__(self, routed: Any) -> None:
        self._routed = routed

    def get_run(self, *, tenant: str, run_id: str) -> dict[str, Any] | None:
        """Read immutable run admission through the caller-routed EG client."""
        from arbora.native_run_admission import require_lookup

        method = require_lookup(self._routed)
        return _run_blocking(lambda: method(tenant=tenant, run_id=run_id))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        method = getattr(self._routed, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            return _run_blocking(lambda: method(*args, **kwargs))

        return call


class ServedEngine:
    """The served process's engine handle over the one EG client pool."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        self.graph_compute = _GraphCompute(pool)

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
        from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

        return CONTROL_GRAPH

    @property
    def work_items(self) -> _BlockingWorkItems:
        """EG WorkItems, held on the tenant's control graph (``__control__``)."""
        return _BlockingWorkItems(
            self.graph_compute.for_graph(self.work_item_graph).async_client.work_items
        )

    async def health(self, graph: str) -> Mapping[str, Any]:
        """EG's own health document, read under the ambient session."""
        return await self.graph_compute.for_graph(graph).async_client.health()

    def _session_client(self) -> SessionRoutedClient:
        return self.graph_compute.for_graph(str(_ambient_session().graph)).async_client

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

        return list(_run_blocking(run))

    async def query_cypher_async(
        self,
        query: str,
        params: Mapping[str, Any] | None = None,
        *,
        session: Any = None,
    ) -> list[Any]:
        """Read through the public client on this loop under one exact caller."""
        from arbora.api.contracts.session import resolve_session

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

        return str(_run_blocking(run))

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

        return bool(_run_blocking(run))

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

        return bool(_run_blocking(run))


def process_engine_pool() -> Any:
    """The one EG client pool of this process, built on first use.

    Raises:
        RuntimeError: ``GRAPH_SERVICE_ENDPOINTS`` names no engine.
    """
    global _POOL
    with _LOCK:
        if _POOL is None:
            from medulla import MedullaClient

            from arbora.core.settings import runtime_config
            from arbora.epistemic import ConnectClient, EpistemicClientPool
            from arbora.mcp_server.served_invocation import engine_endpoint

            endpoints = runtime_config.graph_service_endpoints
            if not endpoints:
                raise RuntimeError(
                    "arbora serves through epistemic-graph: set "
                    "GRAPH_SERVICE_ENDPOINTS to the engine endpoint"
                )
            _POOL = EpistemicClientPool(
                cast(ConnectClient, MedullaClient.connect),
                **engine_endpoint(endpoints),
            )
        return _POOL


def served_engine() -> ServedEngine:
    """The process engine handle over :func:`process_engine_pool`."""
    global _ENGINE
    pool = process_engine_pool()
    with _LOCK:
        if _ENGINE is None or _ENGINE.pool is not pool:
            _ENGINE = ServedEngine(pool)
        return _ENGINE


def reset_process_engine() -> None:
    """Forget the process pool (tests and process teardown only)."""
    global _POOL, _ENGINE
    with _LOCK:
        _POOL = None
        _ENGINE = None
