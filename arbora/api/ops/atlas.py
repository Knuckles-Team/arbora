"""Atlas source connection, mapping and run operations (ARBORA-OPS-R911, R922.3).

Every operation here is registered and refuses ``UNAVAILABLE``: the engine has
no source connection record, mapping state or sync run list yet, and arbora
keeps none. A credential is always a secret reference, never a value, and a
mapping is approved only against the digest the caller read. The shapes the
engine must serve are in the gaps list.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator

from arbora.api.ops._common import Params
from arbora.api.ops._pending import refuse_unavailable, unserved_read, unserved_write
from arbora.api.registry import Effect, OpSpec

handle_unserved = refuse_unavailable
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


class RunParams(Params):
    run_id: str = _Id


class RunsListParams(Params):
    connection_id: str | None = Field(default=None, min_length=1, max_length=256)
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = (
        None
    )
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


def operations() -> tuple[OpSpec, ...]:
    m = __name__

    def read(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
        return unserved_read(
            module=m,
            op_id=op_id,
            summary=summary,
            example=example,
            params=params,
            scope=READ_SCOPE,
        )

    def write(
        op_id: str,
        summary: str,
        example: str,
        params: type[Params],
        effect: Effect = Effect.WRITE,
    ) -> OpSpec:
        return unserved_write(
            module=m,
            op_id=op_id,
            summary=summary,
            example=example,
            params=params,
            scope=WRITE_SCOPE,
            effect=effect,
        )

    return (
        read(
            "ingest.sources.providers.list",
            "List the source providers",
            "which source providers exist",
            ProvidersParams,
        ),
        read(
            "ingest.sources.connections.list",
            "List the source connections",
            "list my source connections",
            ConnectionListParams,
        ),
        read(
            "ingest.sources.connections.get",
            "Read one source connection",
            "show this source connection",
            ConnectionParams,
        ),
        write(
            "ingest.sources.connections.create",
            "Create a source connection from a secret reference",
            "connect this source",
            ConnectionCreateParams,
        ),
        write(
            "ingest.sources.connections.delete",
            "Delete a source connection",
            "remove this source connection",
            ConnectionParams,
            Effect.ADMIN,
        ),
        write(
            "ingest.sources.discover",
            "Discover what a connection offers",
            "discover this source",
            ConnectionParams,
        ),
        read(
            "ingest.sources.mapping.get",
            "Read a connection's mapping and its state",
            "show this source mapping",
            ConnectionParams,
        ),
        write(
            "ingest.sources.mapping.propose",
            "Propose a mapping for a connection",
            "propose a mapping for this source",
            ConnectionParams,
        ),
        write(
            "ingest.sources.mapping.approve",
            "Approve the mapping the caller read",
            "approve this source mapping",
            MappingApproveParams,
            Effect.ADMIN,
        ),
        read(
            "ingest.sources.sync.preview",
            "Preview the changes a sync would make",
            "preview this source sync",
            ConnectionParams,
        ),
        read(
            "ingest.sources.runs.get",
            "Read one source sync run",
            "show this sync run",
            RunParams,
        ),
        write(
            "ingest.sources.runs.cancel",
            "Cancel one source sync run",
            "cancel this sync run",
            RunParams,
        ),
        read(
            "ingest.sources.runs.list",
            "List source sync runs",
            "show the recent source sync runs",
            RunsListParams,
        ),
    )


specs = operations

__all__ = ["handle_unserved", "operations", "specs"]
