"""Optional host-log HTTP adapter; absent qualified composition denies reads."""

import asyncio
import math

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from graph_os.agent_host.log_access import (
    HostLogAccess,
    HostLogRecord,
    HostLogRetentionGap,
)
from graph_os.identity.engine import IdentityUnavailable


class LogReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(default=1, ge=1, le=2)
    log_ref: str = Field(min_length=1, max_length=128)
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=128, ge=1, le=128)


def host_log_credential(request: Request) -> str:
    if request.headers.getlist("cookie") or request.url.query:
        raise HTTPException(403, "workspace_denied")
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        raise HTTPException(403, "workspace_denied")
    scheme, _, token = values[0].partition(" ")
    if scheme.lower() != "bearer" or not token or any(c.isspace() for c in token):
        raise HTTPException(403, "workspace_denied")
    return token


def register_host_log_routes(app: FastAPI, access: HostLogAccess | None) -> None:
    """Attach the same optional read authority to the existing connector host."""

    def available() -> HostLogAccess:
        if access is None:
            raise HTTPException(503, "log_unavailable")
        return access

    @app.post("/host/logs/reference")
    async def reference(request: Request) -> dict:
        service = available()
        credential = host_log_credential(request)
        try:
            async with asyncio.timeout(1):
                value = await service.reference(credential)
        except PermissionError:
            raise HTTPException(403, "workspace_denied") from None
        except (IdentityUnavailable, TimeoutError):
            raise HTTPException(503, "log_unavailable") from None
        except Exception:  # noqa: BLE001 - owner failures must not expose log material
            raise HTTPException(503, "log_unavailable") from None
        return service.reference_binding(value)

    @app.post("/host/logs/read")
    async def read(request: Request, body: LogReadRequest) -> dict:
        service = available()
        credential = host_log_credential(request)
        try:
            async with asyncio.timeout(1):
                batch = await service.read(
                    credential,
                    body.log_ref,
                    cursor=body.cursor,
                    limit=body.limit,
                )
        except HostLogRetentionGap:
            raise HTTPException(409, "retention_gap") from None
        except PermissionError:
            raise HTTPException(403, "workspace_denied") from None
        except (IdentityUnavailable, TimeoutError):
            raise HTTPException(503, "log_unavailable") from None
        except Exception:  # noqa: BLE001 - owner failures must not expose log material
            raise HTTPException(503, "log_unavailable") from None
        if body.version == 2:
            try:
                records = [_metadata(record) for record in batch.records]
            except (ValueError, OverflowError):
                raise HTTPException(503, "log_unavailable") from None
            return {
                "version": 2,
                "records": records,
                "cursor": batch.cursor,
                "gap": batch.gap,
            }
        return {
            "records": [{"message": record.message} for record in batch.records],
            "cursor": batch.cursor,
            "gap": batch.gap,
        }


def _metadata(record: HostLogRecord) -> dict:
    if (
        type(record.sequence) is not int
        or not 1 <= record.sequence <= 2**63 - 1
        or type(record.timestamp) not in (int, float)
        or not math.isfinite(record.timestamp)
        or record.timestamp < 0
        or type(record.request) is not str
        or len(record.request.encode()) > 1024
        or type(record.severity) is not str
        or not 1 <= len(record.severity.encode()) <= 32
    ):
        raise ValueError("invalid log metadata")
    return {
        "host_instance": record.host_instance,
        "sequence": record.sequence,
        "request": record.request,
        "timestamp": record.timestamp,
        "severity": record.severity,
        "message": record.message,
    }
