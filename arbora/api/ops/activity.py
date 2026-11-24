"""City activity: the tenant's agent activity, served from run-trace provenance.

Arbora serves the tenant's agent activity as the engine's run-trace facts,
not as a synthesized feed (ARBORA-OPS-R916). The stream is a cursor-paged
read over run-trace step records (``MEDULLA-ORCH-VOCAB-R803``): each entry
carries the run, the step, the request/argument/result digests and the
provenance of the trace. An event the engine has not recorded is not served,
and arbora invents no activity. A malformed cursor refuses
``INVALID_ARGUMENT`` before any engine read; an uncomposed activity port
fails closed with a typed ``UNAVAILABLE``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, ValidationError

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import FrozenState, Params, build_read_op
from arbora.api.registry import OpSpec

#: The bounded opaque token the engine issues as a page cursor.
CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ActivityStepRecord(FrozenState):
    """One recorded run-trace step fact: digested, with its provenance."""

    run: str
    step: str
    request_digest: str
    argument_digest: str
    result_digest: str
    provenance: str
    observed_at: datetime


class ActivityPageState(FrozenState):
    """One cursor-paged slice of the tenant's recorded run-trace steps."""

    tenant: str
    entries: tuple[ActivityStepRecord, ...] = ()
    next_cursor: str | None = None


@runtime_checkable
class ActivityPort(Protocol):
    """The one typed port Arbora calls the engine run-trace read through.

    The port is composed as ``city_activity`` by the served process. A
    refusal raised by the port (``SUBJECT_ACCESS_DENIED`` for a caller
    without run read access on the tenant's runs) is a closed arbora code.
    """

    async def steps(
        self,
        *,
        tenant: str,
        cursor: str | None,
        limit: int,
        context: ExecutionContext,
    ) -> ActivityPageState: ...


class ActivityStreamParams(Params):
    """One cursor-paged activity read; the tenant is verified, never supplied."""

    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ActivityStreamValue(FrozenState):
    """The served page: recorded step facts and the next engine cursor."""

    tenant: str
    entries: tuple[ActivityStepRecord, ...] = ()
    next_cursor: str | None = None


class ActivityStreamResult(Params):
    value: ActivityStreamValue


async def handle_activity_stream(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Serve one tenant-scoped page of recorded run-trace steps."""
    request = ActivityStreamParams.model_validate(dict(params))
    if request.cursor is not None and not CURSOR_PATTERN.fullmatch(request.cursor):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "cursor"})
    port = context.services.get("city_activity")
    if port is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "run trace engine unbound"})
    raw = await port.steps(
        tenant=context.caller.tenant,
        cursor=request.cursor,
        limit=request.limit,
        context=context,
    )
    try:
        page = (
            raw
            if isinstance(raw, ActivityPageState)
            else ActivityPageState.model_validate(raw)
        )
    except (ValidationError, TypeError):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "run trace page malformed"}
        ) from None
    if page.tenant != context.caller.tenant:
        raise OperationRefused("SUBJECT_ACCESS_DENIED", {"subject": page.tenant})
    value = ActivityStreamValue(
        tenant=page.tenant,
        entries=page.entries,
        next_cursor=page.next_cursor,
    )
    return {"value": value.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="fleet.city.activity",
            summary="Read the tenant's agent activity stream",
            examples=("what have the agents been doing",),
            params=ActivityStreamParams,
            result=ActivityStreamResult,
            handler="arbora.api.ops.activity.handle_activity_stream",
            scope="fleet:read",
        ),
    )
