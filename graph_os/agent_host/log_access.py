"""Host log access over injected identity, workspace policy and record owners.

No grant issuer, credential store or filesystem fallback exists here. Composition
must supply qualified owners; the default host exposes no log data.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from graph_os.identity.ports import SessionAuthority, SessionState


class HostLogDenied(PermissionError):
    """Current identity, workspace grant or reference binding was refused."""


class HostLogRetentionGap(HostLogDenied):
    """The authenticated resume position is no longer retained."""


@dataclass(frozen=True, slots=True)
class WorkspaceLogGrant:
    """Owner decision for one exact session and configured workspace."""

    tenant: str
    principal: str
    session_ref: str
    workspace: str
    policy_revision: str
    revision: str
    expires_at_ms: int
    action: Literal["host.logs.read"]
    source_generations: tuple[str, ...] = ()


def valid_generations(values: tuple[str, ...]) -> bool:
    return (
        type(values) is tuple
        and 1 <= len(values) <= 32
        and all(
            type(value) is str
            and 1 <= len(value) <= 128
            and value not in {"*", "latest", "prior"}
            and not any(
                character.isspace() or ord(character) < 32 for character in value
            )
            for value in values
        )
        and len(set(values)) == len(values)
    )


class WorkspaceGrantProvider(Protocol):
    async def authorize(
        self, session: SessionState, *, workspace: str
    ) -> WorkspaceLogGrant | None:
        """Read current policy; None denies. Never infer a grant from scopes."""
        ...


@dataclass(frozen=True, slots=True)
class HostLogRecord:
    """Already-redacted producer record with an exact private scope."""

    tenant: str
    workspace: str
    host_instance: str
    message: str
    request: str = ""
    sequence: int = 0
    timestamp: float = 0.0
    severity: str = "INFO"


@dataclass(frozen=True, slots=True)
class HostLogBatch:
    """A tenant-local opaque cursor; shared file offsets are forbidden."""

    records: tuple[HostLogRecord, ...]
    cursor: str | None
    gap: bool = False


class HostLogSource(Protocol):
    async def read(
        self,
        *,
        tenant: str,
        workspace: str,
        host_instance: str,
        source_generations: tuple[str, ...],
        cursor: str | None,
        limit: int,
    ) -> HostLogBatch:
        """Return a bounded, redacted tenant projection, never shared bytes."""
        ...


@dataclass(frozen=True, slots=True)
class _Reference:
    grant: WorkspaceLogGrant
    session_expiry: int
    started: bool = False
    cursor: str | None = None


class HostLogAccess:
    """Process-local references with fresh identity and grant checks per read."""

    def __init__(
        self,
        *,
        sessions: SessionAuthority,
        grants: WorkspaceGrantProvider,
        source: HostLogSource,
        workspace: str,
        host_instance: str,
        interface_mode: Literal["terminal", "web", "both"],
        web_logs: bool,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not workspace or not host_instance:
            raise ValueError("configured workspace and host instance required")
        self._sessions = sessions
        self._grants = grants
        self._source = source
        self._workspace = str(Path(workspace).resolve(strict=True))
        if (
            interface_mode not in {"terminal", "web", "both"}
            or type(web_logs) is not bool
        ):
            raise ValueError("invalid_launch")
        self._enabled = interface_mode in {"terminal", "both"} and web_logs
        self._interface_mode = interface_mode
        self._web_logs = web_logs
        self._host = host_instance
        self._clock = clock
        self._references: dict[str, _Reference] = {}
        self._inflight: set[str] = set()

    async def _grant(self, credential: str) -> tuple[SessionState, WorkspaceLogGrant]:
        if not self._enabled:
            raise HostLogDenied("workspace_denied")
        state = await self._sessions.resolve_session(credential)
        now = int(self._clock() * 1000)
        if not isinstance(state, SessionState) or state.expires_at_ms <= now:
            raise HostLogDenied("workspace_denied")
        grant = await self._grants.authorize(state, workspace=self._workspace)
        if not isinstance(grant, WorkspaceLogGrant):
            raise HostLogDenied("workspace_denied")
        refreshed = await self._sessions.resolve_session(credential)
        now = int(self._clock() * 1000)
        if refreshed != state or state.expires_at_ms <= now:
            raise HostLogDenied("workspace_denied")
        expected = (
            state.resolution.request_context["tenant"],
            state.resolution.principal_id,
            state.session_ref,
            self._workspace,
            state.resolution.request_context["policy_version"],
            "host.logs.read",
        )
        actual = (
            grant.tenant,
            grant.principal,
            grant.session_ref,
            grant.workspace,
            grant.policy_revision,
            grant.action,
        )
        if (
            actual != expected
            or not valid_generations(grant.source_generations)
            or not grant.revision
            or type(grant.expires_at_ms) is not int
            or not now < grant.expires_at_ms <= state.expires_at_ms
        ):
            raise HostLogDenied("workspace_denied")
        return state, grant

    async def reference(self, credential: str) -> str:
        """Issue an opaque handle only after a current explicit owner grant."""
        state, grant = await self._grant(credential)
        now = int(self._clock() * 1000)
        self._references = {
            key: value
            for key, value in self._references.items()
            if min(value.grant.expires_at_ms, value.session_expiry) > now
        }
        if len(self._references) >= 1024:
            raise HostLogDenied("log_unavailable")
        reference = secrets.token_urlsafe(32)
        self._references[reference] = _Reference(grant, state.expires_at_ms)
        return reference

    def reference_binding(self, reference: str) -> dict:
        """Describe a just-issued reference for the authenticated HTTP handoff."""
        bound = self._references[reference]
        grant = bound.grant
        return {
            "version": 1,
            "log_ref": reference,
            "host_instance": self._host,
            "workspace": grant.workspace,
            "tenant": grant.tenant,
            "principal": grant.principal,
            "session_ref": grant.session_ref,
            "policy_revision": grant.policy_revision,
            "grant_revision": grant.revision,
            "expires_at_ms": min(grant.expires_at_ms, bound.session_expiry),
            "interface_mode": self._interface_mode,
            "web_logs": self._web_logs,
            "ready": True,
        }

    async def _bound(self, credential: str, reference: str) -> WorkspaceLogGrant:
        state, grant = await self._grant(credential)
        bound = self._references.get(reference)
        if (
            bound is None
            or bound.grant != grant
            or state.expires_at_ms != bound.session_expiry
        ):
            raise HostLogDenied("workspace_denied")
        return grant

    async def read(
        self, credential: str, reference: str, *, cursor: str | None, limit: int
    ) -> HostLogBatch:
        """Recheck after source awaits, so revocation suppresses pending bytes."""
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("invalid_limit")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
            raise ValueError("invalid_cursor")
        grant = await self._bound(credential, reference)
        if reference in self._inflight:
            raise HostLogDenied("log_unavailable")
        self._inflight.add(reference)
        try:
            return await self._read_bound(credential, reference, grant, cursor, limit)
        finally:
            self._inflight.discard(reference)

    async def _read_bound(
        self,
        credential: str,
        reference: str,
        grant: WorkspaceLogGrant,
        cursor: str | None,
        limit: int,
    ) -> HostLogBatch:
        bound = self._references[reference]
        if bound.started and cursor != bound.cursor:
            raise HostLogDenied("workspace_denied")
        try:
            batch = await self._source.read(
                tenant=grant.tenant,
                workspace=self._workspace,
                host_instance=self._host,
                source_generations=grant.source_generations,
                cursor=cursor,
                limit=limit,
            )
        except HostLogRetentionGap:
            await self._bound(credential, reference)
            raise

        await self._bound(credential, reference)
        self._validate_batch(batch, grant, limit)
        self._references[reference] = replace(bound, cursor=batch.cursor, started=True)
        return batch

    def _validate_batch(
        self, batch: HostLogBatch, grant: WorkspaceLogGrant, limit: int
    ) -> None:
        if not isinstance(batch, HostLogBatch) or len(batch.records) > limit:
            raise HostLogDenied("log_unavailable")
        if type(batch.gap) is not bool or (
            batch.cursor is not None
            and (not isinstance(batch.cursor, str) or len(batch.cursor) > 1024)
        ):
            raise HostLogDenied("log_unavailable")
        for record in batch.records:
            if (
                not isinstance(record, HostLogRecord)
                or (record.tenant, record.workspace) != (grant.tenant, self._workspace)
                or record.host_instance not in grant.source_generations
                or not isinstance(record.message, str)
                or len(record.message.encode("utf-8")) > 16 * 1024
            ):
                raise HostLogDenied("log_unavailable")
