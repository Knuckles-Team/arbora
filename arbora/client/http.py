"""HTTP transport for :func:`arbora.client.invoke.invoke` (ARBORA-OPS-R067.3).

A caller outside the served process, such as a skill script, reaches one
operation with ``POST /api/v1/ops/<op_id>`` and a bearer token. A refused
operation raises :class:`OperationError` carrying the operation's own code,
so a caller can report it without guessing from transport status.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from .invoke import Transport

__all__ = ["HttpTransport", "OperationError"]


class OperationError(Exception):
    """The served operation refused the call with ``code``."""

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


class HttpTransport(Transport):
    """POST one operation's params to a served Arbora REST gateway."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 330.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an absolute http(s) URL")
        if not token:
            raise ValueError("a bearer token is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport

    async def invoke(self, op_id: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        async with httpx.AsyncClient(
            timeout=self._timeout_s, transport=self._transport
        ) as client:
            response = await client.post(
                f"{self._base_url}/api/v1/ops/{op_id}",
                json=dict(params),
                headers={"Authorization": f"Bearer {self._token}"},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OperationError(
                "UNAVAILABLE", {"status": response.status_code}
            ) from exc
        if not isinstance(payload, Mapping):
            raise OperationError("UNAVAILABLE", {"status": response.status_code})
        error = payload.get("error")
        if not payload.get("ok") or isinstance(error, Mapping):
            details = error if isinstance(error, Mapping) else {}
            raise OperationError(str(details.get("code") or "UNAVAILABLE"), details)
        return payload
