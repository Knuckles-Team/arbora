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
from collections.abc import Mapping, Sequence
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


class FleetSourceExecutor:
    """Run source reads for one verified caller through ``fleet.call``'s gateway."""

    def __init__(self, gateway: Any, caller: Any, *, overall_timeout_s: float = 60.0):
        self._gateway = gateway
        self._caller = caller
        self._overall = overall_timeout_s

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
    "FleetSourceExecutor",
    "SourceReadRequest",
    "SourceReadResult",
]
