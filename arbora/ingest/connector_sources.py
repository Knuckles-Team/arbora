"""Connector sources served through the composed SDK sync runner (ARBORA-OPS-R020.6).

:class:`ConnectorIngestRunner` is the ``ingest_runner`` service for connector
sources. It lists each fleet server's declared sources through that server's
``connector://resources`` resource (the fleet catalog plus the
connector's own declaration, no engine listing method), and syncs one source
as one engine WorkItem: submit, claim, run
:class:`~agent_connector_sdk.ingest.source_sync.SourceSyncRunner` with every
tool call admitted through the governed fleet gateway, then commit the outcome.
Status comes from the shared engine KV checkpoint the SDK runner writes.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from agent_connector_sdk.ingest.service import KnowledgeIngest
from agent_connector_sdk.ingest.source_sync import (
    KvSourceCheckpoints,
    SourceSyncError,
    SourceSyncRunner,
)
from agent_connector_sdk.ingest.sources import IngestSourceDeclaration
from agent_connector_sdk.ingest.transport import EpistemicGraphIngestTransport

from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet.declarations import RESOURCES_URI, parse_declaration
from arbora.ingest.service import (
    IngestJobRecord,
    IngestPackInventory,
    IngestPackRecord,
    IngestSourceInventory,
    IngestSourceRecord,
    IngestSyncReceipt,
)

__all__ = ["SOURCE_SYNC_KIND", "ConnectorIngestRunner", "FleetSourceSession"]

#: The prefix of a failed sync's WorkItem ``error_ref``.
_ERROR_REF = "ingest-source-error"


def _error_ref(reason: str | None, engine_code: str | None) -> str | None:
    """The WorkItem ``error_ref`` of a failed sync: its reason and engine code."""
    if not reason:
        return None
    return f"{_ERROR_REF}:{reason}" + (f":{engine_code}" if engine_code else "")


def _parse_error_ref(value: Any) -> tuple[str | None, str | None]:
    """The reason and engine code a job's ``error_ref`` carries."""
    if not isinstance(value, str) or not value.startswith(_ERROR_REF + ":"):
        return None, None
    _, _, rest = value.partition(":")
    reason, _, engine_code = rest.partition(":")
    return reason or None, engine_code or None


#: The engine WorkItem kind of one connector source sync.
SOURCE_SYNC_KIND = "arbora.ingest.source_sync"
_LEASE_S = 600.0
_SEAM_TIMEOUT_S = 60.0
#: The fleet catalog page size (its maximum) and page bound for one listing.
_CATALOG_PAGE = 100
_MAX_CATALOG_PAGES = 50

ClientFactory = Callable[[str], Awaitable[Any]]

logger = logging.getLogger(__name__)


@contextmanager
def _caller_session(caller: Any) -> Iterator[None]:
    """Bind the verified caller's session; the WorkItem seam reads it."""
    session = getattr(caller, "session", None)
    if session is None:
        yield
        return
    from arbora.api.contracts.session import use_session

    with use_session(session):
        yield


class _LoopBoundNamespace:
    """A sync view of an async engine namespace, for a call in a worker thread.

    The WorkItem seam (``arbora.work_items``) is synchronous; the served
    engine client is async. Each call runs on the serving loop while the
    worker thread waits for its result.
    """

    def __init__(self, namespace: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._namespace = namespace
        self._loop = loop

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._namespace, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            result = method(*args, **kwargs)
            if not inspect.isawaitable(result):
                return result
            return asyncio.run_coroutine_threadsafe(
                _awaited(result), self._loop
            ).result(_SEAM_TIMEOUT_S)

        return call


async def _awaited(value: Any) -> Any:
    return await value


class _SyncEngine:
    """The ``engine`` shape the WorkItem seam reads: one ``work_items``."""

    def __init__(self, client: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.work_items = _LoopBoundNamespace(client.work_items, loop)


async def _seam(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one WorkItem seam call in a worker thread (contextvars carried)."""
    import functools

    import anyio

    return await anyio.to_thread.run_sync(functools.partial(call, *args, **kwargs))


def _split(source_id: str) -> tuple[str, str]:
    server, sep, source = source_id.partition("/")
    if not sep or not server or not source or "/" in source:
        raise OperationRefused(
            "NOT_FOUND", {"reason": "source id is <server>/<source>"}
        )
    return server, source


def _payload(result: Any) -> Any:
    """The JSON value of a fleet call result (structured content or the value)."""
    if isinstance(result, Mapping):
        for key in ("structured_content", "structuredContent", "result", "value"):
            if isinstance(result.get(key), Mapping):
                return result[key]
    return result


def _knowledge_ingest(client: Any) -> KnowledgeIngest:
    """The SDK ingest service over one engine client (``SourceIngest``)."""
    return KnowledgeIngest(EpistemicGraphIngestTransport(client))


class FleetSourceSession:
    """An SDK :class:`SourceSession` over the governed fleet gateway call path."""

    def __init__(self, fleet: Any, caller: Any, server: str) -> None:
        self._fleet = fleet
        self._caller = caller
        self._server = server

    async def tool_is_read_only(self, name: str) -> bool:
        # The gateway refuses any call whose admitted effect is not READ.
        return bool(name)

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        from arbora.api.registry import Effect

        result = await self._fleet.call(
            self._caller,
            self._server,
            name,
            dict(arguments),
            expected_effect=Effect.READ,
        )
        return _payload(result)


class ConnectorIngestRunner:
    """The ``ingest_runner`` service for declared connector sources.

    Args:
        fleet: The governed fleet gateway (``LateFleetGateway``).
        data_client: ``tenant -> client`` on the tenant graph (SourceIngest, KV).
        control_client: ``tenant -> client`` on the control graph (WorkItems).
    """

    def __init__(
        self,
        *,
        fleet: Any,
        data_client: ClientFactory,
        control_client: ClientFactory,
        ingest_for: Callable[[Any], Any] | None = None,
    ) -> None:
        self._fleet = fleet
        self._data_client = data_client
        self._control_client = control_client
        self._ingest_for = ingest_for or _knowledge_ingest

    async def _declarations(self, caller: Any) -> dict[str, IngestSourceDeclaration]:
        """Each fleet server's declaration, read from ``connector://resources``."""
        items: list[Mapping[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_CATALOG_PAGES):
            params: dict[str, Any] = {"limit": _CATALOG_PAGE}
            if cursor:
                params["cursor"] = cursor
            found = await self._fleet.resources_list(caller, **params)
            items.extend(found.get("items", ()))
            cursor = found.get("next_cursor")
            if not cursor:
                break
        servers = sorted(
            {
                str(item.get("server"))
                for item in items
                if item.get("uri") == RESOURCES_URI and item.get("server")
            }
        )
        declarations: dict[str, IngestSourceDeclaration] = {}
        for server in servers:
            read = await self._fleet.resources_read(
                caller, server=server, uri=RESOURCES_URI
            )
            payload = parse_declaration(str(_payload(read).get("text") or "{}"))
            declarations[server] = IngestSourceDeclaration.model_validate(payload)
        return declarations

    async def _declared(self, caller: Any, source_id: str) -> tuple[Any, Any]:
        server, source = _split(source_id)
        declaration = (await self._declarations(caller)).get(server)
        spec = None if declaration is None else declaration.source(source)
        if declaration is None or spec is None:
            raise OperationRefused("NOT_FOUND", {"reason": "source is not declared"})
        return declaration, spec

    async def _record(
        self, tenant: str, server: str, declaration: Any, spec: Any
    ) -> IngestSourceRecord:
        checkpoints = KvSourceCheckpoints(await self._data_client(tenant))
        state, _ = await checkpoints.load(declaration.connector, spec.source_id)
        return IngestSourceRecord(
            source_id=f"{server}/{spec.source_id}",
            tenant=tenant,
            state="error" if state.state == "failed" else "active",
            connector=declaration.connector,
            record_type=spec.record_type,
            incremental=spec.incremental,
            cursor=state.cursor,
            records=state.records,
            last_job_id=state.last_job_id,
            sync_state=state.state,
            sync_reason=(state.last_report or {}).get("reason"),
            sync_engine_code=(state.last_report or {}).get("engine_code"),
        )

    async def list_sources(
        self, *, tenant: str, caller: Any = None
    ) -> IngestSourceInventory:
        declarations = await self._declarations(caller)
        records = [
            await self._record(tenant, server, declaration, spec)
            for server, declaration in sorted(declarations.items())
            for spec in declaration.sources
        ]
        return IngestSourceInventory(sources=tuple(records))

    async def get_source_status(
        self, *, tenant: str, source_id: str, caller: Any = None
    ) -> IngestSourceRecord:
        declaration, spec = await self._declared(caller, source_id)
        return await self._record(tenant, _split(source_id)[0], declaration, spec)

    async def sync_source(
        self,
        *,
        tenant: str,
        source_id: str,
        mode: str,
        idempotency_key: str,
        caller: Any = None,
    ) -> IngestSyncReceipt:

        declaration, spec = await self._declared(caller, source_id)
        from arbora.work_items import WorkItemBackendUnavailable

        try:
            with _caller_session(caller):
                return await self._run(
                    declaration, spec, tenant, source_id, mode, idempotency_key, caller
                )
        except WorkItemBackendUnavailable as exc:
            raise OperationRefused("UNAVAILABLE", {"reason": str(exc)}) from exc

    async def _run(
        self,
        declaration: Any,
        spec: Any,
        tenant: str,
        source_id: str,
        mode: str,
        idempotency_key: str,
        caller: Any,
    ) -> IngestSyncReceipt:
        from arbora import work_items

        server = _split(source_id)[0]
        control = _SyncEngine(
            await self._control_client(tenant), asyncio.get_running_loop()
        )
        key = hashlib.sha256(
            f"{source_id}|{mode}|{idempotency_key}".encode()
        ).hexdigest()
        item_id, created = await _seam(
            work_items.submit_work_item_atomic,
            control,
            kind=SOURCE_SYNC_KIND,
            idempotency_key=key,
            tenant=tenant,
            description=f"sync {source_id}",
            metadata={"source_id": source_id, "mode": mode},
        )
        if not created:
            current = await _seam(work_items.get_work_item, control, item_id) or {}
            return self._receipt(
                item_id, source_id, tenant, mode, current.get("status")
            )
        claim = await _seam(
            work_items.claim_specific,
            control,
            item_id,
            token=f"arbora-ingest-{uuid.uuid4().hex}",
            lease_ttl_s=_LEASE_S,
        )
        if claim is None:
            return self._receipt(item_id, source_id, tenant, mode, "queued")
        client = await self._data_client(tenant)
        runner = SourceSyncRunner(
            connector=declaration.connector,
            server=server,
            session=FleetSourceSession(self._fleet, caller, server),
            ingest=self._ingest_for(client),
            checkpoints=KvSourceCheckpoints(client),
        )
        try:
            report = await runner.sync(spec, mode=mode, job_id=item_id)
        except SourceSyncError as exc:
            # The receipt carries only the reason code; keep the cause visible
            # (tool name and exception type, never arguments or results).
            logger.warning("source sync %s failed: %s: %s", source_id, exc.reason, exc)
            await _seam(
                work_items.commit_result,
                control,
                item_id,
                claim,
                outcome="failed",
                error_ref=_error_ref(exc.reason, None),
            )
            return self._receipt(
                item_id, source_id, tenant, mode, "failed", reason=exc.reason
            )
        outcome = "failed" if report.state == "failed" else "succeeded"
        if outcome == "failed":
            # ARBORA-OPS-R020.6.8: a failed run names its cause, never a payload.
            logger.warning(
                "source sync %s failed: reason=%s engine_code=%s records_seen=%d",
                source_id,
                report.reason,
                report.engine_code,
                report.records_seen,
            )
        await _seam(
            work_items.commit_result,
            control,
            item_id,
            claim,
            outcome=outcome,
            result_ref=f"ingest-source:{report.state}:{report.submitted}",
            error_ref=_error_ref(report.reason, report.engine_code),
        )
        return self._receipt(
            item_id,
            source_id,
            tenant,
            mode,
            outcome,
            reason=report.reason,
            engine_code=report.engine_code,
        )

    @staticmethod
    def _receipt(
        item_id: str,
        source_id: str,
        tenant: str,
        mode: str,
        status: Any,
        *,
        reason: str | None = None,
        engine_code: str | None = None,
    ) -> IngestSyncReceipt:
        mapped = {"succeeded": "completed", "failed": "failed", "running": "running"}
        return IngestSyncReceipt(
            job_id=item_id,
            source_id=source_id,
            tenant=tenant,
            mode="full" if mode == "full" else "incremental",
            status=mapped.get(str(status), "queued"),  # type: ignore[arg-type]
            reason=reason,
            engine_code=engine_code,
        )

    async def index_repository(self, **_: Any) -> Any:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "repository indexing is not composed"}
        )

    async def list_packs(
        self, *, tenant: str, caller: Any = None
    ) -> IngestPackInventory:
        """One pack per connector server that declares ingest sources.

        A pack is in ``error`` when any of its sources is, else ``active``.
        """
        declarations = await self._declarations(caller)
        packs = []
        for server, declaration in sorted(declarations.items()):
            records = [
                await self._record(tenant, server, declaration, spec)
                for spec in declaration.sources
            ]
            failed = any(record.state == "error" for record in records)
            packs.append(
                IngestPackRecord(
                    pack_id=server,
                    tenant=tenant,
                    state="error" if failed else "active",
                )
            )
        return IngestPackInventory(packs=tuple(packs))

    async def get_job_status(
        self, *, tenant: str, job_id: str, caller: Any = None
    ) -> IngestJobRecord:
        from arbora import work_items

        control = _SyncEngine(
            await self._control_client(tenant), asyncio.get_running_loop()
        )
        with _caller_session(caller):
            item = await _seam(work_items.get_work_item, control, job_id)
        if item is None:
            raise OperationRefused("NOT_FOUND", {"reason": "job is not known"})
        mapped = {"succeeded": "completed", "failed": "failed", "leased": "running"}
        reason, engine_code = _parse_error_ref(item.get("error_ref"))
        return IngestJobRecord(
            job_id=job_id,
            tenant=tenant,
            status=mapped.get(str(item.get("status")), "queued"),  # type: ignore[arg-type]
            reason=reason,
            engine_code=engine_code,
        )
