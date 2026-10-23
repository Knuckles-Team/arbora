"""Generated identity client adapters; unsupported published contracts refuse."""

import asyncio
from typing import Any

from .approval import OriginalSessionSnapshot, RegisteredApprovalKey
from .engine import IdentityUnavailable, SessionRecord, _shape, _strings, _text


async def identity_value(
    client: Any, graph: str, operation: dict[str, Any], kind: str
) -> Any:
    """Use the owning generated validator, preserving raw response field types."""
    from epistemic_graph.generated.security import decode_identity, send_identity

    try:
        async with asyncio.timeout(1):
            response = await send_identity(client, {"op": operation}, graph)
            decode_identity(response)
        payload = response.payload
        _shape(payload, frozenset({"kind", "value"}), frozenset())
        if payload["kind"] != kind:
            raise IdentityUnavailable("identity response kind disagrees")
        return payload["value"]
    except PermissionError:
        raise
    except Exception:
        raise IdentityUnavailable("qualified identity operation unavailable") from None


def approval_key(value: Any) -> RegisteredApprovalKey:
    """Parse exact native key facts, without inferring ownership from key bytes."""
    _shape(value, frozenset(RegisteredApprovalKey.__dataclass_fields__), frozenset())
    try:
        return RegisteredApprovalKey(**dict(value))
    except (TypeError, ValueError):
        raise IdentityUnavailable("invalid approval key record") from None


class EngineOriginalSessionResolver:
    """Read through a caller-bound native client; never synthesize its stamp."""

    def __init__(self, client: Any, *, graph: str, tenant: str) -> None:
        self._client = client
        self._graph = _text(graph)
        self._tenant = _text(tenant)

    async def resolve_original_session(self, reference: str) -> OriginalSessionSnapshot:
        value = await identity_value(
            self._client,
            self._graph,
            {
                "family": "session",
                "op": "inspect_reference",
                "session_ref": _text(reference),
                "tenant": self._tenant,
            },
            "session_authority",
        )
        return self._snapshot(value, reference)

    def _snapshot(self, value: Any, reference: str) -> OriginalSessionSnapshot:
        _shape(
            value, frozenset({"record", "scopes", "authority_revision"}), frozenset()
        )
        try:
            snapshot = OriginalSessionSnapshot(
                SessionRecord.parse(value["record"]),
                _strings(value["scopes"], scopes=True),
                value["authority_revision"],
            )
        except (TypeError, ValueError):
            raise IdentityUnavailable("invalid session authority facts") from None
        if (
            snapshot.record.session_ref != reference
            or snapshot.record.tenant != self._tenant
        ):
            raise PermissionError("original session binding disagrees")
        return snapshot

    async def renew_original_session(
        self, expected: OriginalSessionSnapshot
    ) -> OriginalSessionSnapshot:
        """Request native idle-only CAS; never renew signed approval evidence."""
        before = expected.record
        if before.tenant != self._tenant:
            raise PermissionError("original session tenant disagrees")
        value = await identity_value(
            self._client,
            self._graph,
            {
                "family": "session",
                "op": "renew_reference",
                "session_ref": before.session_ref,
                "tenant": before.tenant,
                "expected_authority_revision": expected.authority_revision,
                "expected_scopes": list(expected.scopes),
            },
            "session_authority",
        )
        after = self._snapshot(value, before.session_ref)
        record = after.record
        if (
            record.principal_id != before.principal_id
            or record.absolute_expires_at_ms != before.absolute_expires_at_ms
            or record.mfa_at_ms != before.mfa_at_ms
            or record.idle_expires_at_ms <= before.idle_expires_at_ms
            or record.idle_expires_at_ms > record.absolute_expires_at_ms
            or after.scopes != expected.scopes
            or after.authority_revision <= expected.authority_revision
        ):
            # Native mutation may have committed. Never retry or claim rollback.
            raise PermissionError("native session renewal result disagrees")
        return after


class EngineHumanApprovalKeys:
    """Use the existing broker's native read authority; no human authority issued."""

    def __init__(self, client: Any, *, graph: str, tenant: str) -> None:
        self._client = client
        self._graph = _text(graph)
        self._tenant = _text(tenant)

    async def resolve_key(
        self, key_id: str, *, principal_id: str, purpose: str
    ) -> RegisteredApprovalKey:
        value = await identity_value(
            self._client,
            self._graph,
            {
                "family": "credential",
                "op": "inspect_approval_key",
                "request": {
                    "key_id": _text(key_id),
                    "principal_id": _text(principal_id),
                    "purpose": purpose,
                },
            },
            "approval_key",
        )
        key = approval_key(value)
        if (key.key_id, key.principal_id, key.tenant, key.purpose) != (
            key_id,
            principal_id,
            self._tenant,
            purpose,
        ):
            raise PermissionError("registered key binding disagrees")
        return key
