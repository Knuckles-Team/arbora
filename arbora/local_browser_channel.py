"""Private same-socket local provenance, without browser operation authority."""

import asyncio
import secrets
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from arbora.access.action_policy import ActionPolicy
from arbora.api.contracts.session import GraphSession, resolve_session
from arbora.browser_control.browser_control_common import canonical_json
from arbora.browser_control.browser_control_descriptor import BrowserToolDescriptor
from arbora.browser_control.browser_control_validation import validated_catalog_digests
from arbora.identity.browser import _header_digests
from arbora.identity.local_browser import LocalBrowserTransport
from arbora.mcp_server.served_engine import ServedEngine

_CHANNEL_LIFETIME_SECONDS = 60


def _snapshot(scope: Any, key: bytes) -> tuple[Any, ...]:
    if (
        scope.get("type") != "websocket"
        or type(scope.get("path")) is not str
        or type(scope.get("raw_path")) is not bytes
        or type(scope.get("query_string")) is not bytes
    ):
        raise PermissionError("Exact WebSocket target required")
    return (
        scope["type"],
        scope["path"],
        scope["raw_path"],
        scope.get("root_path", ""),
        scope["query_string"],
        scope.get("scheme"),
        tuple(scope.get("client") or ()),
        tuple(scope.get("server") or ()),
        *_header_digests(scope, key),
    )


def _catalog_snapshot(tools: tuple[Any, ...]) -> str:
    if type(tools) is not tuple or len(tools) > 64:
        raise ValueError("Bounded exact local catalog required")
    copies = tuple(
        BrowserToolDescriptor.model_validate(tool.model_dump(mode="json"))
        for tool in tools
    )
    if len({tool.tool_id for tool in copies}) != len(copies):
        raise ValueError("Unique local tool identifiers required")
    validated_catalog_digests(copies)
    return canonical_json([tool.model_dump(mode="json") for tool in copies])


@dataclass(frozen=True, slots=True, repr=False)
class _LocalSocket:
    scope: Any
    task: Any
    session: GraphSession
    snapshot: tuple[Any, ...]
    key: bytes
    lease_id: str
    grant: dict[str, Any]
    expires_at_ms: int
    lease_snapshot: str | None = None


class LocalBrowserChannelOwner:
    """Own transient socket identity; EG owns bounded durable lease state.

    No public receipt, scope field or native lease can recreate this owner's
    private record. This component exposes no tool registration or dispatch.
    """

    def __init__(
        self,
        engine: ServedEngine,
        policy: ActionPolicy,
        transport: LocalBrowserTransport,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(engine, ServedEngine)
            or not isinstance(policy, ActionPolicy)
            or policy.engine is not engine
        ):
            raise TypeError("Canonical served state and action policy required")
        if not isinstance(transport, LocalBrowserTransport):
            raise TypeError("Explicit local browser transport required")
        self._engine = engine
        self._state = engine.browser_control_state
        self._policy, self._transport = policy, transport
        self._clock = clock
        self._sockets: dict[int, _LocalSocket] = {}
        self._pending: dict[int, Any] = {}

    def _local(self, scope: Any) -> _LocalSocket:
        if self._policy.engine is not self._engine:
            raise PermissionError("Local browser policy owner changed")
        record = self._sockets.get(id(scope))
        if (
            record is None
            or record.scope is not scope
            or record.task is not asyncio.current_task()
        ):
            raise PermissionError("No owner-bound local browser socket")
        resolve_session(record.session, required_scope="kg:write")
        self._transport.validate(scope, record.session)
        if _snapshot(scope, record.key) != record.snapshot:
            raise PermissionError("Local browser socket changed")
        if int(self._clock() * 1000) >= record.expires_at_ms:
            raise PermissionError("Local browser socket expired")
        return record

    async def _native(self, operation: Callable[..., Any], *, write: bool) -> Any:
        async with asyncio.timeout(5):
            return await self._state._call_async(
                operation,
                required_scope="kg:write" if write else "kg:read",
            )

    async def _policy_revision(self) -> str:
        if self._policy.engine is not self._engine:
            raise PermissionError("Local browser policy owner changed")
        async with asyncio.timeout(5):
            revision = await self._retained_policy_revision()
        if self._policy.engine is not self._engine:
            raise PermissionError("Local browser policy owner changed")
        return revision

    async def _retained_policy_revision(self) -> str:
        source = asyncio.create_task(self._policy.qualified_source_revision_async())
        cancelled = None
        while not source.done():
            try:
                await asyncio.shield(source)
            except asyncio.CancelledError as error:
                cancelled = cancelled or error
                self._invalidate_current_task()
                source.cancel()
            except Exception:  # noqa: BLE001 - retrieve canonical redacted refusal
                break
        if cancelled is not None:
            if not source.cancelled():
                source.exception()
            raise cancelled
        return source.result()

    def _invalidate_current_task(self) -> None:
        task = asyncio.current_task()
        for key, record in tuple(self._sockets.items()):
            if record.task is task:
                del self._sockets[key]

    async def validate(self, scope: Any) -> None:
        record = self._sockets.get(id(scope))
        try:
            await self._validate(scope)
        except BaseException:
            if (
                record is not None
                and record.task is asyncio.current_task()
                and self._sockets.get(id(scope)) is record
            ):
                del self._sockets[id(scope)]
            raise

    async def _validate(self, scope: Any) -> None:
        """Recheck the owner-private socket and native liveness; grant nothing."""
        record = self._local(scope)
        revision = await self._policy_revision()
        self._local(scope)
        if revision != record.grant["policy_revision"]:
            raise PermissionError("Configured local browser policy changed")
        lease = await self._native(
            lambda client: client.control_leases.get(
                tenant=record.session.tenant, lease_id=record.lease_id
            ),
            write=False,
        )
        self._local(scope)
        if (
            not isinstance(lease, dict)
            or canonical_json(lease) != record.lease_snapshot
        ):
            raise PermissionError("Local browser native lease is not live or exact")
        revision = await self._policy_revision()
        self._local(scope)
        if revision != record.grant["policy_revision"]:
            raise PermissionError("Configured local browser policy changed")
        latest = await self._native(
            lambda client: client.control_leases.get(
                tenant=record.session.tenant, lease_id=record.lease_id
            ),
            write=False,
        )
        self._local(scope)
        if (
            not isinstance(latest, dict)
            or canonical_json(latest) != record.lease_snapshot
        ):
            raise PermissionError(
                "Local browser native lease changed during policy read"
            )

    async def _retire(self, record: _LocalSocket) -> None:
        resolve_session(record.session, required_scope="kg:write")
        self._transport.validate(record.scope, record.session)
        lease = await self._native(
            lambda client: client.control_leases.get(
                tenant=record.session.tenant, lease_id=record.lease_id
            ),
            write=False,
        )
        resolve_session(record.session, required_scope="kg:write")
        self._transport.validate(record.scope, record.session)
        if not isinstance(lease, dict) or lease.get("grant") != record.grant:
            raise PermissionError("Local browser retirement outcome unresolved")
        if lease.get("status") in {"revoked", "expired"}:
            return
        result = await self._native(
            lambda client: client.control_leases.transition(
                tenant=record.session.tenant,
                lease_id=record.lease_id,
                expected_revision=lease["revision"],
                to="revoked",
                idempotency_key=record.lease_id + ":retire",
            ),
            write=True,
        )
        resolve_session(record.session, required_scope="kg:write")
        if not isinstance(result, dict) or result.get("outcome") != "applied":
            raise PermissionError("Local browser retirement outcome unresolved")

    @asynccontextmanager
    async def channel(
        self, scope: Any, session: GraphSession, tools: tuple[Any, ...]
    ) -> AsyncIterator[None]:
        if id(scope) in self._pending or len(self._pending) >= 16:
            raise PermissionError(
                "Local browser socket already pending or capacity exhausted"
            )
        self._pending[id(scope)] = scope
        try:
            async with asyncio.timeout(_CHANNEL_LIFETIME_SECONDS):
                async with self._channel(scope, session, tools):
                    yield
        finally:
            if self._pending.get(id(scope)) is scope:
                del self._pending[id(scope)]

    async def _prepare(
        self, scope: Any, session: GraphSession, tools: tuple[Any, ...]
    ) -> tuple[_LocalSocket, str, int]:
        if scope.get("type") != "websocket" or id(scope) in self._sockets:
            raise PermissionError("One new local WebSocket required")
        resolve_session(session, required_scope="kg:write")
        self._transport.validate(scope, session)
        catalog_snapshot = _catalog_snapshot(tools)
        catalog_digest, scope_digest = validated_catalog_digests(tools)
        key = secrets.token_bytes(32)
        snapshot = _snapshot(scope, key)
        revision = await self._policy_revision()
        self._transport.validate(scope, session)
        if _snapshot(scope, key) != snapshot:
            raise PermissionError("Local browser socket changed during policy read")
        if _catalog_snapshot(tools) != catalog_snapshot:
            raise PermissionError("Local browser catalog changed during admission")
        now = int(self._clock() * 1000)
        lease_id = "local_channel_" + secrets.token_hex(24)
        grant = {
            "evidence_kind": "local_operator_anonymous",
            "principal": "anonymous",
            "document_reference": "document_" + secrets.token_hex(24),
            "origin": self._transport.origin,
            "catalog_digest": catalog_digest,
            "tool_scope_digest": scope_digest,
            "policy_revision": revision,
            "operation_authority": False,
        }
        record = _LocalSocket(
            scope,
            asyncio.current_task(),
            session,
            snapshot,
            key,
            lease_id,
            grant,
            now + int(_CHANNEL_LIFETIME_SECONDS * 1000),
        )
        return record, catalog_snapshot, now

    async def _issue(
        self, record: _LocalSocket, now: int
    ) -> tuple[Any, asyncio.CancelledError | None]:
        issuance = asyncio.create_task(
            self._native(
                lambda client: client.control_leases.issue(
                    tenant=record.session.tenant,
                    lease_id=record.lease_id,
                    kind="browser.local-channel",
                    grant=record.grant,
                    issued_at_ms=now,
                    expires_at_ms=record.expires_at_ms,
                    hard_expires_at_ms=record.expires_at_ms,
                    idempotency_key=record.lease_id,
                ),
                write=True,
            )
        )
        cancelled = None
        try:
            while True:
                try:
                    result = await asyncio.shield(issuance)
                    return result, cancelled
                except asyncio.CancelledError as error:
                    self._invalidate_current_task()
                    if issuance.cancelled():
                        raise
                    cancelled = cancelled or error
        except (Exception, asyncio.CancelledError) as error:
            primary = cancelled or error
            from medulla.client import EngineResponseError

            # A native idempotency conflict is a definite refusal, not our lease.
            if (
                not isinstance(error, EngineResponseError)
                or getattr(error, "code", None) != "IDEMPOTENCY_CONFLICT"
            ):
                await self._cleanup(record, primary)
            raise primary from None

    async def _cleanup(
        self, record: _LocalSocket, primary: BaseException | None = None
    ) -> None:
        try:
            await self._retire(record)
        except asyncio.CancelledError as cancelled:
            refusal = (
                primary if isinstance(primary, asyncio.CancelledError) else cancelled
            )
            refusal.add_note("Native local lease retirement remains unresolved")
            raise refusal from None
        except Exception:
            if primary is None:
                raise
            primary.add_note(
                "Native local lease issuance/retirement remains unresolved"
            )

    @staticmethod
    def _issued_record(record: _LocalSocket, result: Any, now: int) -> _LocalSocket:
        lease = result.get("lease")
        expected = {
            "lease_id": record.lease_id,
            "status": "active",
            "kind": "browser.local-channel",
            "grant": record.grant,
            "issued_at_ms": now,
            "expires_at_ms": record.expires_at_ms,
            "hard_expires_at_ms": record.expires_at_ms,
        }
        if (
            not isinstance(lease, dict)
            or any(lease.get(k) != v for k, v in expected.items())
            or type(lease.get("revision")) is not int
            or lease["revision"] <= 0
        ):
            raise PermissionError("Native issuance snapshot is not exact")
        return replace(record, lease_snapshot=canonical_json(lease))

    @asynccontextmanager
    async def _channel(
        self, scope: Any, session: GraphSession, tools: tuple[Any, ...]
    ) -> AsyncIterator[None]:
        record, catalog_snapshot, now = await self._prepare(scope, session, tools)
        self._sockets[id(scope)] = record
        issued = False
        try:
            self._local(scope)
            result, cancelled = await self._issue(record, now)
            if not isinstance(result, dict) or result.get("outcome") != "issued":
                raise PermissionError("Native local channel lease was not issued")
            issued = True
            if cancelled is not None:
                raise cancelled
            self._local(scope)
            record = self._issued_record(record, result, now)
            self._sockets[id(scope)] = record
            await self.validate(scope)
            if _catalog_snapshot(tools) != catalog_snapshot:
                raise PermissionError("Local browser catalog changed during admission")
            yield
        finally:
            if self._sockets.get(id(scope)) is record:
                del self._sockets[id(scope)]
            if issued:
                await self._cleanup(record, sys.exception())
