"""Virtual-graph source executor over the governed ``fleet.call`` path.

ARBORA-FLEET-R043.7. The engine reads a connector's data at query time and
asks arbora to run each source read (EG-FEDERATED-QUERY-R722). This executor
routes every read through :meth:`FleetGateway.call`, so each read is admitted
under the caller's own delegation: the gateway rechecks scope, policy and the
tool's current effect at dispatch. Only READ tools run; a tool whose effect
changed refuses. Reads are bounded by pages, rows and a per-source and overall
timeout. One source failing returns its own error and never fails the others.
Rows are returned to the engine and are not stored or logged here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from arbora.api.registry import Effect

MAX_PAGES_CAP = 20
MAX_ROWS_CAP = 10_000
MAX_SOURCES = 32
MAX_CONCURRENCY = 8


@dataclass(frozen=True)
class SourceReadRequest:
    """One source read: a connector tool, its arguments and its page contract."""

    source_id: str
    connector: str
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    rows_key: str = "items"
    cursor_key: str = "next_cursor"
    cursor_arg: str = "cursor"
    max_pages: int = 5
    max_rows: int = 1000
    timeout_s: float = 30.0


@dataclass(frozen=True)
class SourceReadResult:
    """Rows read from one source, or the reason it could not be read."""

    source_id: str
    rows: tuple[Any, ...] = ()
    pages: int = 0
    truncated: bool = False
    error_code: str | None = None
    error_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


@dataclass(frozen=True)
class ForeignReadSourceRequest:
    """One approved connector source read sent through EG's four phases.

    ``contract`` is the SDK ``AccessContract`` parsed from the connector pack.
    Its predicates are the only source fields copied into EG rows.  The
    approval and both digests are producer DTOs supplied by the caller; this
    executor never creates an authority record or derives a replacement digest.
    """

    source_id: str
    connector: str
    tool: str
    source_name: str
    contract: Any
    approval: Any
    plan_digest: str
    query_digest: str
    limits: Any
    arguments: Mapping[str, Any] = field(default_factory=dict)
    rows_key: str = "items"
    cursor_key: str = "next_cursor"
    cursor_arg: str = "cursor"
    max_pages: int = 5
    max_rows: int = 1000
    timeout_s: float = 30.0


@dataclass(frozen=True)
class ForeignReadResult:
    """The bounded source lifecycle and optional correlated query result."""

    source_id: str
    execution: Any | None = None
    rows: int = 0
    pages: int = 0
    truncated: bool = False
    query_result: Any | None = None
    error_code: str | None = None
    error_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None


def _page(payload: Any, rows_key: str) -> list[Any]:
    if isinstance(payload, Mapping):
        value = payload.get("value", payload)
        if isinstance(value, Mapping) and rows_key in value:
            value = value[rows_key]
        elif isinstance(payload, Mapping) and rows_key in payload:
            value = payload[rows_key]
        payload = value
    if isinstance(payload, list):
        return payload
    raise ValueError("the source page has no row list")


def _next_cursor(payload: Any, key: str) -> str | None:
    if isinstance(payload, Mapping):
        for scope in (payload, payload.get("value")):
            if isinstance(scope, Mapping) and scope.get(key):
                return str(scope[key])
    return None


def _failure(
    request: SourceReadRequest, code: str, reason: str, **kw: Any
) -> SourceReadResult:
    return SourceReadResult(
        source_id=request.source_id, error_code=code, error_reason=reason, **kw
    )


def _foreign_failure(
    request: ForeignReadSourceRequest,
    code: str,
    reason: str,
    *,
    execution: Any | None = None,
    rows: int = 0,
    pages: int = 0,
    truncated: bool = False,
    query_result: Any | None = None,
) -> ForeignReadResult:
    return ForeignReadResult(
        source_id=request.source_id,
        execution=execution,
        rows=rows,
        pages=pages,
        truncated=truncated,
        query_result=query_result,
        error_code=code,
        error_reason=reason,
    )


def _foreign_row(row: Any, request: ForeignReadSourceRequest) -> dict[str, Any]:
    """Map one connector row through the pack-declared source fields."""

    if not isinstance(row, Mapping):
        raise ValueError("source row is not an object")
    key_field = getattr(request.contract, "key_field", None)
    contract_iri = getattr(request.contract, "contract_iri", None)
    predicates = getattr(request.contract, "predicates", ())
    if (
        not isinstance(key_field, str)
        or not key_field
        or not isinstance(contract_iri, str)
        or not contract_iri
    ):
        raise ValueError("source contract has no stable key or mapping identity")
    raw_id = row.get(key_field)
    if raw_id is None:
        raise ValueError("source row has no declared key")
    columns: dict[str, Any] = {}
    for predicate in predicates:
        if not isinstance(predicate, Sequence) or len(predicate) != 2:
            raise ValueError("source contract predicate is malformed")
        _property_iri, source_field = predicate
        if not isinstance(source_field, str) or not source_field:
            raise ValueError("source contract field is malformed")
        if source_field in row:
            columns[source_field] = row[source_field]
    score = row.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score = None
    return {
        "id": str(raw_id),
        "score": float(score) if score is not None else None,
        "columns": columns,
        "provenance": [
            f"connector:{request.connector}",
            f"contract:{contract_iri}",
            f"source:{request.source_id}",
        ],
    }


def _foreign_error(exc: BaseException) -> tuple[str, str]:
    """Map connector/SDK failures to the existing four source error codes."""

    if isinstance(exc, PermissionError):
        return "SOURCE_NOT_GRANTED", "the caller has no current grant for this source"
    if isinstance(exc, TimeoutError):
        return "SOURCE_TIMEOUT", "the foreign source read timed out"
    reason = getattr(exc, "reason", None)
    if reason == "deadline":
        return "SOURCE_TIMEOUT", "the foreign source read timed out"
    if reason == "transport":
        return "SOURCE_UNAVAILABLE", "the foreign-read transport was unavailable"
    if isinstance(reason, str):
        return "SOURCE_BAD_RESPONSE", f"the foreign-read phase was refused ({reason})"
    if isinstance(exc, ValueError):
        return (
            "SOURCE_BAD_RESPONSE",
            "the source row or foreign-read request was invalid",
        )
    return (
        "SOURCE_UNAVAILABLE",
        f"the foreign source read failed ({type(exc).__name__})",
    )


class FleetSourceExecutor:
    """Run source reads for one verified caller through ``fleet.call``'s gateway."""

    def __init__(
        self,
        gateway: Any,
        caller: Any,
        *,
        overall_timeout_s: float = 60.0,
        foreign_client: Any | None = None,
    ):
        self._gateway = gateway
        self._caller = caller
        self._overall = overall_timeout_s
        self._foreign_client = foreign_client

    @classmethod
    def from_engine(
        cls,
        gateway: Any,
        caller: Any,
        engine: Any,
        *,
        graph: str,
        overall_timeout_s: float = 60.0,
    ) -> FleetSourceExecutor:
        """Compose the SDK receiver with Arbora's already-routed EG client.

        The SDK adapter resolves EG's generated senders and keeps the
        authenticated request envelope on that side of the boundary.  Arbora
        only chooses the existing graph-scoped client; it never calls a
        private client transport or adds authority fields.
        """

        try:
            from agent_connector_sdk.adapters.foreign_read import (
                ForeignReadClient,
                NativeRequestResponseAdapter,
            )
        except ImportError:
            return cls(
                gateway,
                caller,
                overall_timeout_s=overall_timeout_s,
            )
        engine_client = engine.graph_compute.for_graph(graph).async_client
        return cls(
            gateway,
            caller,
            overall_timeout_s=overall_timeout_s,
            foreign_client=ForeignReadClient(
                NativeRequestResponseAdapter(engine_client, graph=graph)
            ),
        )

    async def read(self, request: SourceReadRequest) -> SourceReadResult:
        pages = max(1, min(request.max_pages, MAX_PAGES_CAP))
        cap = max(1, min(request.max_rows, MAX_ROWS_CAP))
        rows: list[Any] = []
        cursor: str | None = None
        done = 0
        try:
            async with asyncio.timeout(request.timeout_s):
                while done < pages:
                    arguments = dict(request.arguments)
                    if cursor:
                        arguments[request.cursor_arg] = cursor
                    payload = await self._gateway.call(
                        self._caller,
                        request.connector,
                        request.tool,
                        arguments,
                        expected_effect=Effect.READ,
                    )
                    done += 1
                    rows.extend(_page(payload, request.rows_key))
                    cursor = _next_cursor(payload, request.cursor_key)
                    if len(rows) >= cap:
                        return SourceReadResult(
                            request.source_id,
                            tuple(rows[:cap]),
                            done,
                            truncated=len(rows) > cap or cursor is not None,
                        )
                    if not cursor:
                        break
        except TimeoutError:
            return _failure(
                request,
                "SOURCE_TIMEOUT",
                "the source read timed out",
                rows=tuple(rows),
                pages=done,
                truncated=True,
            )
        except PermissionError:
            return _failure(
                request,
                "SOURCE_NOT_GRANTED",
                "the caller has no current grant for this source",
            )
        except ValueError:
            return _failure(
                request,
                "SOURCE_BAD_RESPONSE",
                "the source page was not a row list",
                pages=done,
            )
        except Exception as exc:
            return _failure(
                request,
                "SOURCE_UNAVAILABLE",
                f"the source read failed ({type(exc).__name__})",
                pages=done,
            )
        return SourceReadResult(
            request.source_id, tuple(rows), done, truncated=cursor is not None
        )

    async def read_foreign(
        self,
        request: ForeignReadSourceRequest,
        *,
        correlate: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> ForeignReadResult:
        """Read pages, publish them through EG and optionally run the query.

        The correlation callback receives only EG's opaque execution handle
        after ``Complete`` succeeds.  A caller can use it to invoke the
        generated correlated query without giving this Arbora module a second
        engine transport or a copied query DTO.
        """

        if self._foreign_client is None:
            return _foreign_failure(
                request,
                "SOURCE_UNAVAILABLE",
                "the foreign-read SDK adapter is unavailable",
            )
        try:
            from agent_connector_sdk.adapters.foreign_read import (
                AbortRequest,
                ChunkRequest,
                CompleteRequest,
                ForeignReadLimits,
                OpenRequest,
            )
        except ImportError:
            return _foreign_failure(
                request,
                "SOURCE_UNAVAILABLE",
                "the foreign-read SDK adapter is unavailable",
            )
        execution: Any | None = None
        sequence: int | None = None
        pages = 0
        rows = 0
        truncated = False
        try:
            limits = ForeignReadLimits.model_validate(request.limits)
            opened = await self._foreign_client.open(
                OpenRequest(
                    schema_version=1,
                    source_name=request.source_name,
                    approval=request.approval,
                    plan_digest=request.plan_digest,
                    query_digest=request.query_digest,
                    limits=limits,
                )
            )
            execution = opened.execution
            sequence = opened.next_sequence
            cursor: str | None = None
            page_cap = max(1, min(request.max_pages, MAX_PAGES_CAP))
            row_cap = max(1, min(request.max_rows, limits.max_rows, MAX_ROWS_CAP))
            async with asyncio.timeout(request.timeout_s):
                while pages < page_cap and rows < row_cap:
                    arguments = dict(request.arguments)
                    if cursor:
                        arguments[request.cursor_arg] = cursor
                    payload = await self._gateway.call(
                        self._caller,
                        request.connector,
                        request.tool,
                        arguments,
                        expected_effect=Effect.READ,
                    )
                    pages += 1
                    source_rows = _page(payload, request.rows_key)
                    next_cursor = _next_cursor(payload, request.cursor_key)
                    remaining = row_cap - rows
                    if len(source_rows) > remaining:
                        source_rows = source_rows[:remaining]
                        truncated = True
                    elif next_cursor is not None and len(source_rows) + rows >= row_cap:
                        truncated = True
                    mapped = [
                        _foreign_row(source_row, request) for source_row in source_rows
                    ]
                    for offset in range(0, len(mapped), limits.chunk_rows):
                        chunk_rows = mapped[offset : offset + limits.chunk_rows]
                        result = await self._foreign_client.chunk(
                            ChunkRequest(
                                execution=execution,
                                sequence=sequence,
                                rows=chunk_rows,
                            )
                        )
                        sequence = result.next_sequence
                        rows += len(chunk_rows)
                    cursor = next_cursor
                    if truncated or not cursor:
                        break
                if cursor is not None and pages >= page_cap and rows < row_cap:
                    truncated = True
                await self._foreign_client.complete(
                    CompleteRequest(execution=execution, sequence=sequence)
                )
                query_result = await correlate(execution) if correlate else None
            return ForeignReadResult(
                source_id=request.source_id,
                execution=execution,
                rows=rows,
                pages=pages,
                truncated=truncated,
                query_result=query_result,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, reason = _foreign_error(exc)
            if execution is not None and sequence is not None:
                with contextlib.suppress(Exception):
                    await self._foreign_client.abort(
                        AbortRequest(
                            execution=execution,
                            sequence=sequence,
                            reason="source_failed",
                        )
                    )
            return _foreign_failure(
                request,
                code,
                reason,
                execution=execution,
                rows=rows,
                pages=pages,
                truncated=truncated,
            )

    async def read_all(
        self, requests: Sequence[SourceReadRequest]
    ) -> tuple[SourceReadResult, ...]:
        """Read sources concurrently; the result order matches the request order."""
        if len(requests) > MAX_SOURCES:
            raise ValueError("too many sources in one read")
        gate = asyncio.Semaphore(MAX_CONCURRENCY)

        async def one(request: SourceReadRequest) -> SourceReadResult:
            async with gate:
                return await self.read(request)

        tasks = [asyncio.ensure_future(one(r)) for r in requests]
        _, pending = await asyncio.wait(tasks, timeout=self._overall)
        out: list[SourceReadResult] = []
        for request, task in zip(requests, tasks, strict=True):
            if task in pending:
                task.cancel()
                out.append(
                    _failure(
                        request, "SOURCE_TIMEOUT", "the overall read deadline passed"
                    )
                )
            else:
                out.append(task.result())
        return tuple(out)


__all__ = [
    "ForeignReadResult",
    "ForeignReadSourceRequest",
    "FleetSourceExecutor",
    "SourceReadRequest",
    "SourceReadResult",
]
