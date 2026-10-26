"""Optional host-log HTTP adapter; absent qualified composition denies reads."""

import asyncio
import ipaddress
import math
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from arbora.agent_host.log_access import (
    HostLogAccess,
    HostLogRecord,
    HostLogRetentionGap,
    _normalize_endpoint,
)
from arbora.deployment.host_config import load_hosting_deployment_settings
from arbora.identity.engine import IdentityUnavailable
from arbora.security.http_boundary import normalize_host_authorities


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


_FORWARDED_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-proto",
)


def _loopback_host(value: str) -> bool:
    rendered = str(value or "").strip().strip("[]").split("%", 1)[0]
    if rendered.casefold() in {"localhost", "localhost."}:
        return True
    try:
        return ipaddress.ip_address(rendered).is_loopback
    except ValueError:
        return False


def _configured_public_endpoint(request: Request) -> str | None:
    """Read only server-owned endpoint context, never request headers."""
    value = getattr(request.app.state, "public_base_url", None)
    if value is None:
        try:
            value = load_hosting_deployment_settings().public_base_url
        except (TypeError, ValueError):
            return ""
    if type(value) is not str or not value.strip():
        return None
    return value


def _trusted_request_origin(request: Request) -> str:
    """Return the exact origin a TUI receipt may bind to.

    A configured public endpoint is authoritative. Without one, only a
    loopback request may derive an origin from its single Host header. The
    host application's Uvicorn boundary deliberately does not trust forwarded
    headers, so accepting them here would create a second, weaker proxy path.
    """
    if any(request.headers.getlist(name) for name in _FORWARDED_HEADERS):
        raise HTTPException(403, "workspace_denied")
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        raise HTTPException(403, "workspace_denied")

    configured = _configured_public_endpoint(request)
    if configured is not None:
        try:
            endpoint = _normalize_endpoint(configured)
            parsed = urlsplit(endpoint)
            expected_authority = next(iter(normalize_host_authorities([parsed.netloc])))
            observed_authority = next(iter(normalize_host_authorities(hosts)))
            observed = _normalize_endpoint(f"{parsed.scheme}://{observed_authority}/")
        except (StopIteration, TypeError, ValueError):
            raise HTTPException(503, "log_unavailable") from None
        if observed != endpoint or observed_authority != expected_authority:
            raise HTTPException(403, "workspace_denied")
        return endpoint

    peer = request.client
    if peer is None or not _loopback_host(peer.host):
        raise HTTPException(403, "workspace_denied")
    try:
        endpoint = _normalize_endpoint(str(request.base_url))
        if not _loopback_host(urlsplit(endpoint).hostname or ""):
            raise ValueError("non-loopback host")
    except (TypeError, ValueError):
        raise HTTPException(403, "workspace_denied") from None
    return endpoint


def register_host_log_routes(app: FastAPI, access: HostLogAccess | None) -> None:
    """Attach the same optional read authority to the existing connector host."""

    def available() -> HostLogAccess:
        if access is None:
            raise HTTPException(503, "log_unavailable")
        return access

    @app.post("/host/logs/reference")
    async def reference(request: Request) -> dict:
        service = available()
        endpoint = _trusted_request_origin(request)
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
        try:
            return service.reference_binding(value, endpoint=endpoint)
        except (KeyError, ValueError):
            raise HTTPException(503, "log_unavailable") from None

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
