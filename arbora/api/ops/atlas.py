"""Atlas source connection, mapping and run operations (ARBORA-OPS-R911, R922.3).

The engine holds the records: ``SourceConnection*``, ``SourceMapping*`` and
``ConnectorRun*``. Arbora keeps none, and reaches each only through the
medulla pin-lag guard, so a pinned engine without the method refuses
``UNAVAILABLE`` naming it. Discover, the sync preview, the provider list and a
mapping proposal ask the source connector through ``fleet.call`` under the
caller's identity; discover then records its job on the connection. A
credential is always a secret reference, never a value, and a mapping is
approved only against the digest the caller read.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, field_validator

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send, rfc3339
from arbora.api.ops._nested import invoke_registered
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import Effect, OpSpec, Verb
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

READ_SCOPE = "ingest:read"
WRITE_SCOPE = "ingest:write"
_REF = r"^(env|openbao)://[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}$"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key)"
)
_Id = Field(min_length=1, max_length=256)


class ProvidersParams(Params):
    pass


class ConnectionListParams(Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class ConnectionParams(Params):
    connection_id: str = _Id


class ConnectionCreateParams(Params):
    provider: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=256)
    settings: dict[str, str | int | float | bool] = Field(
        default_factory=dict, max_length=32
    )
    credential_ref: str = Field(pattern=_REF)

    @field_validator("settings")
    @classmethod
    def _no_secret_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        named = sorted(key for key in value if _SECRET_KEY.search(key))
        if named:
            raise ValueError(f"settings hold no secrets: use credential_ref ({named})")
        return value


class MappingApproveParams(ConnectionParams):
    mapping_digest: str = Field(pattern=_DIGEST)


class MappingProposeParams(ConnectionParams):
    mapping: dict[str, Any] | None = None


class RunParams(Params):
    run_id: str = _Id


class RunsListParams(Params):
    connection_id: str | None = Field(default=None, min_length=1, max_length=256)
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = (
        None
    )
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


ATLAS_SERVER = "atlas"
_CONNECTION = "SourceConnectionControl"
_MAPPING = "SourceMappingControl"
_RUN_RECORD = "ConnectorRunRecord"


def _need(*methods: str) -> None:
    try:
        require_methods(*methods)
    except PinLagUnavailable as error:
        raise refusal(error) from error


async def _engine(
    context: Any, method: str, params: dict[str, Any], *, write: bool = False
) -> dict[str, Any]:
    _need(method)
    answer = await engine_send(context, method, params, idempotent=write)
    return dict(answer) if isinstance(answer, Mapping) else {}


def connection_id_of(tenant: str, params: Mapping[str, Any]) -> str:
    """One stable id for one tenant, provider and label, so a retry is the same record."""
    digest = hashlib.sha256(
        f"{tenant}\0{params['provider']}\0{params['label']}".encode()
    ).hexdigest()
    return f"conn-{digest[:24]}"


def _connection_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in ("connection_id", "provider", "label", "settings", "state")
        if key in row
    } | {"credential_ref": row.get("credential_ref")}


def _run_view(row: Mapping[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        "run_id": row["run_id"],
        "connection_id": row["connection_id"],
        "state": row["state"],
        "started_at": rfc3339(int(row["started_ms"])),
        "counts": row.get("counts", {}),
    }
    if row.get("finished_ms") is not None:
        view["finished_at"] = rfc3339(int(row["finished_ms"]))
    return view


async def _connection(context: Any, connection_id: str) -> dict[str, Any]:
    row = await _engine(
        context, "SourceConnectionGet", {"connection_id": connection_id}
    )
    found = row.get("connection", row)
    if not found.get("connection_id"):
        raise OperationRefused("CONNECTION_NOT_FOUND")
    return dict(found)


async def _connector(
    context: Any, server: str, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """One connector tool through ``fleet.call`` as the caller; its answer is a mapping."""
    reply = await invoke_registered(
        context, "fleet.call", {"server": server, "tool": tool, "arguments": arguments}
    )
    value = reply.get("value", reply) if isinstance(reply, Mapping) else None
    if isinstance(value, Mapping) and set(value) == {"error"}:
        # The fleet has no such server or tool: refuse, never fabricate an answer.
        raise OperationRefused(
            "UNAVAILABLE", {"reason": f"the fleet does not serve {server} {tool}"}
        )
    if not isinstance(value, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": f"the connector answered {tool} with no object"}
        )
    return dict(value)


async def handle_providers_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    answer = await _connector(context, ATLAS_SERVER, "providers", {})
    providers = answer.get("providers", [])
    return {"value": {"providers": providers if isinstance(providers, list) else []}}


async def handle_connections_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request: dict[str, Any] = {"limit": params["limit"]}
    if params.get("cursor"):
        request["cursor"] = params["cursor"]
    page = await _engine(context, "SourceConnectionList", request)
    rows = page.get("connections", [])
    return {
        "value": {
            "connections": [_connection_view(row) for row in rows],
            "next_cursor": page.get("next_cursor"),
        }
    }


async def handle_connections_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {
        "value": _connection_view(await _connection(context, params["connection_id"]))
    }


async def _put(context: Any, connection: dict[str, Any]) -> dict[str, Any]:
    return await _engine(
        context,
        _CONNECTION,
        {"op": {"operation": "put", "connection": connection}},
        write=True,
    )


async def handle_connections_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    connection_id = connection_id_of(str(context.caller.tenant), params)
    await _put(
        context,
        {
            "connection_id": connection_id,
            "provider": params["provider"],
            "label": params["label"],
            "settings": dict(params["settings"]),
            "credential_ref": params["credential_ref"],
        },
    )
    stored = await _connection(context, connection_id)
    return {"value": {"connection_id": connection_id, "state": stored.get("state")}}


async def handle_connections_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    await _connection(context, params["connection_id"])
    answer = await _engine(
        context,
        _CONNECTION,
        {"op": {"operation": "delete", "connection_id": params["connection_id"]}},
        write=True,
    )
    return {
        "value": {
            "connection_id": params["connection_id"],
            "deleted": answer.get("deleted", True),
        }
    }


async def handle_discover(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Ask the provider connector to discover, then record the job on the connection."""
    connection = await _connection(context, params["connection_id"])
    answer = await _connector(
        context,
        str(connection["provider"]),
        "discover",
        {
            "connection_id": connection["connection_id"],
            "settings": connection["settings"],
        },
    )
    job_id = answer.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the connector returned no job id"}
        )
    await _put(
        context,
        {
            "connection_id": connection["connection_id"],
            "provider": connection["provider"],
            "label": connection["label"],
            "settings": {**connection["settings"], "discovery_job_id": job_id},
            "credential_ref": connection["credential_ref"],
        },
    )
    return {"value": {"job_id": job_id}}


async def handle_mapping_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    row = await _engine(
        context, "SourceMappingGet", {"connection_id": params["connection_id"]}
    )
    if not row.get("digest"):
        raise OperationRefused("MAPPING_NOT_FOUND")
    return {
        "value": {
            "mapping": row.get("mapping", {}),
            "state": row.get("state"),
            "digest": row["digest"],
        }
    }


async def handle_mapping_propose(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Propose the caller's mapping, or the one the provider connector derives."""
    mapping = params.get("mapping")
    if mapping is None:
        connection = await _connection(context, params["connection_id"])
        answer = await _connector(
            context,
            str(connection["provider"]),
            "propose_mapping",
            {"connection_id": connection["connection_id"]},
        )
        mapping = answer.get("mapping")
        if not isinstance(mapping, Mapping):
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the connector proposed no mapping"}
            )
    outcome = await _engine(
        context,
        _MAPPING,
        {
            "op": {
                "operation": "propose",
                "connection_id": params["connection_id"],
                "mapping": dict(mapping),
            }
        },
        write=True,
    )
    return {
        "value": {
            "mapping": outcome.get("mapping"),
            "state": outcome.get("state"),
            "digest": outcome.get("digest"),
        }
    }


async def handle_mapping_approve(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Approve against the digest the caller read; a stale digest is the engine's ``MAPPING_CHANGED``."""
    outcome = await _engine(
        context,
        _MAPPING,
        {
            "op": {
                "operation": "approve",
                "connection_id": params["connection_id"],
                "mapping_digest": params["mapping_digest"],
            }
        },
        write=True,
    )
    return {"value": {"state": outcome.get("state")}}


async def handle_sync_preview(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    connection = await _connection(context, params["connection_id"])
    answer = await _connector(
        context,
        str(connection["provider"]),
        "sync_preview",
        {"connection_id": connection["connection_id"]},
    )
    changes = answer.get("changes", [])
    counts = answer.get("counts", {})
    return {
        "value": {
            "changes": changes if isinstance(changes, list) else [],
            "counts": counts,
        }
    }


async def _run(context: Any, run_id: str) -> dict[str, Any]:
    row = await _engine(context, "ConnectorRunGet", {"run_id": run_id})
    found = row.get("run", row)
    if not found.get("run_id"):
        raise OperationRefused("CONNECTOR_RUN_NOT_FOUND")
    return dict(found)


async def handle_runs_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return {"value": _run_view(await _run(context, params["run_id"]))}


async def handle_runs_cancel(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The control plane reports a cancel as a run record in state ``cancelled``."""
    run = await _run(context, params["run_id"])
    outcome = await _engine(
        context,
        _RUN_RECORD,
        {
            "run": {
                "run_id": run["run_id"],
                "connection_id": run["connection_id"],
                "state": "cancelled",
                "counts": run.get("counts", {}),
            }
        },
        write=True,
    )
    return {
        "value": {"run_id": run["run_id"], "state": outcome.get("state", "cancelled")}
    }


async def handle_runs_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    limit = int(params["limit"])
    request: dict[str, Any] = {"limit": limit + 1}
    for key in ("connection_id", "state", "cursor"):
        if params.get(key):
            request[key] = params[key]
    if params.get("state") == "queued":
        return {"value": {"runs": [], "truncated": False, "next_cursor": None}}
    page = await _engine(context, "ConnectorRuns", request)
    rows = page.get("runs", [])
    return {
        "value": {
            "runs": [_run_view(row) for row in rows[:limit]],
            "truncated": len(rows) > limit,
            "next_cursor": page.get("next_cursor"),
        }
    }


#: Writes replay under the engine method that holds the record, never here.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "ingest.sources.connections.create": f"medulla:{_CONNECTION}",
    "ingest.sources.discover": f"medulla:{_CONNECTION}",
    "ingest.sources.mapping.propose": f"medulla:{_MAPPING}",
    "ingest.sources.runs.cancel": f"medulla:{_RUN_RECORD}",
}


def operations() -> tuple[OpSpec, ...]:
    def read(
        op_id: str, summary: str, example: str, params: type[Params], handler: str
    ) -> OpSpec:
        return build_read_op(
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=EngineReply,
            handler=f"{__name__}.{handler}",
            scope=READ_SCOPE,
        )

    def write(
        op_id: str,
        summary: str,
        example: str,
        params: type[Params],
        handler: str,
        effect: Effect = Effect.WRITE,
    ) -> OpSpec:
        return build_write_op(
            verb=Verb.ACT,
            effect=effect,
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=EngineReply,
            handler=f"{__name__}.{handler}",
            scope=WRITE_SCOPE,
        )

    return (
        read(
            "ingest.sources.providers.list",
            "List the source providers",
            "which source providers exist",
            ProvidersParams,
            "handle_providers_list",
        ),
        read(
            "ingest.sources.connections.list",
            "List the source connections",
            "list my source connections",
            ConnectionListParams,
            "handle_connections_list",
        ),
        read(
            "ingest.sources.connections.get",
            "Read one source connection",
            "show this source connection",
            ConnectionParams,
            "handle_connections_get",
        ),
        write(
            "ingest.sources.connections.create",
            "Create a source connection from a secret reference",
            "connect this source",
            ConnectionCreateParams,
            "handle_connections_create",
        ),
        write(
            "ingest.sources.connections.delete",
            "Delete a source connection",
            "remove this source connection",
            ConnectionParams,
            "handle_connections_delete",
            Effect.ADMIN,
        ),
        write(
            "ingest.sources.discover",
            "Discover what a connection offers",
            "discover this source",
            ConnectionParams,
            "handle_discover",
        ),
        read(
            "ingest.sources.mapping.get",
            "Read a connection's mapping and its state",
            "show this source mapping",
            ConnectionParams,
            "handle_mapping_get",
        ),
        write(
            "ingest.sources.mapping.propose",
            "Propose a mapping for a connection",
            "propose a mapping for this source",
            MappingProposeParams,
            "handle_mapping_propose",
        ),
        write(
            "ingest.sources.mapping.approve",
            "Approve the mapping the caller read",
            "approve this source mapping",
            MappingApproveParams,
            "handle_mapping_approve",
            Effect.ADMIN,
        ),
        read(
            "ingest.sources.sync.preview",
            "Preview the changes a sync would make",
            "preview this source sync",
            ConnectionParams,
            "handle_sync_preview",
        ),
        read(
            "ingest.sources.runs.get",
            "Read one source sync run",
            "show this sync run",
            RunParams,
            "handle_runs_get",
        ),
        write(
            "ingest.sources.runs.cancel",
            "Cancel one source sync run",
            "cancel this sync run",
            RunParams,
            "handle_runs_cancel",
        ),
        read(
            "ingest.sources.runs.list",
            "List source sync runs",
            "show the recent source sync runs",
            RunsListParams,
            "handle_runs_list",
        ),
    )


specs = operations

__all__ = ["NATIVE_IDEMPOTENCY", "operations", "specs"]
