"""Hosted client sessions (ARBORA-IDENTITY-R026.1, R026.2, R026.5, R026.7).

One MCP session of one verified client is one engine node with the label
``ClientSession`` (``MEDULLA-IDENTITY-R705``). Arbora holds no session store: the node is
the record. A caller reads only its own sessions; a foreign or unknown id answers with the
one ``CLIENT_NOT_FOUND`` denial.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "CLIENT_SESSION_LABEL",
    "ClientSession",
    "ClientSessionRefused",
    "SessionNodeEngine",
    "record_session_open",
    "session_node_id",
    "visible_sessions",
]

CLIENT_SESSION_LABEL = "ClientSession"


class ClientSessionRefused(Exception):
    """A refusal; the text is the stable code alone."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def session_node_id(tenant: str, session_id: str) -> str:
    return f"client_session:{tenant}:{session_id}"


def _field(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ClientSessionRefused("CLIENT_SESSION_FIELD_MISSING")
    return value


@dataclass(frozen=True, slots=True)
class ClientSession:
    principal: str
    tenant: str
    session_id: str
    client: str
    started_at_ms: int

    def __post_init__(self) -> None:
        for name in ("principal", "tenant", "session_id", "client"):
            _field(getattr(self, name))
        if type(self.started_at_ms) is not int or self.started_at_ms < 0:
            raise ClientSessionRefused("CLIENT_SESSION_FIELD_MISSING")

    @classmethod
    def from_verified_caller(
        cls,
        caller: Any,
        *,
        session_id: str,
        client: str = "",
        now_ms: int | None = None,
    ) -> ClientSession:
        """Build the record from verified claims only; an unsigned principal refuses."""
        if not getattr(caller, "authenticated", False) or getattr(
            caller, "authority_mode", "verified"
        ) not in ("verified", None):
            raise ClientSessionRefused("CLIENT_SESSION_UNSIGNED_PRINCIPAL")
        principal = getattr(caller, "principal", None) or getattr(
            caller, "actor_id", ""
        )
        tenant = getattr(caller, "tenant", None) or getattr(caller, "tenant_id", "")
        if not principal or not tenant:
            raise ClientSessionRefused("CLIENT_SESSION_UNSIGNED_PRINCIPAL")
        return cls(
            principal=str(principal),
            tenant=str(tenant),
            session_id=session_id,
            client=client or str(principal),
            started_at_ms=int(time.time() * 1000) if now_ms is None else now_ms,
        )

    @property
    def node_id(self) -> str:
        return session_node_id(self.tenant, self.session_id)

    def properties(self) -> dict[str, Any]:
        return {
            "principal": self.principal,
            "tenant": self.tenant,
            "session_id": self.session_id,
            "client": self.client,
            "started_at_ms": self.started_at_ms,
        }


class SessionNodeEngine(Protocol):
    """The engine node write the MCP runtime serves (``ServedEngine.create_node``)."""

    def create_node(
        self, node_id: str, label: str, properties: Mapping[str, Any] | None = None
    ) -> bool: ...


async def record_session_open(
    caller: Any,
    *,
    session_id: str,
    engine: SessionNodeEngine,
    client: str = "",
    clock_ms: Callable[[], int] | None = None,
) -> bool:
    """Write the session node once; a repeated ``initialize`` writes nothing new.

    The write is a create-if-absent on the session id, so replay is the engine's
    (``CreateNodeIfAbsent``). An unverified caller opens no session.
    """
    record = ClientSession.from_verified_caller(
        caller,
        session_id=session_id,
        client=client,
        now_ms=None if clock_ms is None else clock_ms(),
    )
    return bool(
        await asyncio.to_thread(
            engine.create_node,
            record.node_id,
            CLIENT_SESSION_LABEL,
            record.properties(),
        )
    )


def visible_sessions(
    rows: list[Mapping[str, Any]],
    *,
    principal: str,
    tenant: str,
    admin: bool,
) -> list[Mapping[str, Any]]:
    """Own sessions only, or every row of the tenant for the exact admin scope."""
    return [
        row
        for row in rows
        if row.get("tenant") == tenant and (admin or row.get("principal") == principal)
    ]
