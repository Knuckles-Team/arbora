"""Optional host-log HTTP adapter; absent qualified composition denies reads."""

import asyncio

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from graph_os.agent_host.log_access import HostLogAccess
from graph_os.identity.engine import IdentityUnavailable


class LogReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    log_ref: str = Field(min_length=1, max_length=128)
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=128, ge=1, le=128)


def _credential(request: Request) -> str:
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
        credential = _credential(request)
        try:
            async with asyncio.timeout(1):
                value = await service.reference(credential)
        except PermissionError:
            raise HTTPException(403, "workspace_denied") from None
        except (IdentityUnavailable, TimeoutError):
            raise HTTPException(503, "log_unavailable") from None
        except Exception:  # noqa: BLE001 - owner failures must not expose log material
            raise HTTPException(503, "log_unavailable") from None
        return {"log_ref": value}

    @app.post("/host/logs/read")
    async def read(request: Request, body: LogReadRequest) -> dict:
        service = available()
        credential = _credential(request)
        try:
            async with asyncio.timeout(1):
                batch = await service.read(
                    credential,
                    body.log_ref,
                    cursor=body.cursor,
                    limit=body.limit,
                )
        except PermissionError:
            raise HTTPException(403, "workspace_denied") from None
        except (IdentityUnavailable, TimeoutError):
            raise HTTPException(503, "log_unavailable") from None
        except Exception:  # noqa: BLE001 - owner failures must not expose log material
            raise HTTPException(503, "log_unavailable") from None
        return {
            "records": [{"message": record.message} for record in batch.records],
            "cursor": batch.cursor,
            "gap": batch.gap,
        }
