"""Supplied-owner snapshot checks, never approval or execution authority.

The caller supplies qualified original-reference and approval-purpose key owners.
Two reads detect observed changes; they cannot prevent a subsequent race.
"""

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

from .engine import IdentityUnavailable, SessionRecord, _strings, _text
from .ports import SessionAuthority, SessionState


def _timestamp(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 2**64 - 1:
        raise ValueError("owner timestamp must be a uint64")


def _key_revision(value: int) -> None:
    _timestamp(value)
    if value == 0:
        raise ValueError("key revision must be positive")


def _reference(value: str) -> None:
    if len(_text(value)) > 512:
        raise ValueError("authority reference exceeds bound")


@dataclass(frozen=True, slots=True, repr=False)
class OriginalHumanBinding:
    """Trusted pending-call metadata; never construct this from wire authority."""

    session_ref: str
    tenant: str
    human: str
    authority_revision: int
    key_id: str
    key_revision: int
    approval_expires_at_ms: int

    def __post_init__(self) -> None:
        for value in (self.session_ref, self.tenant, self.human, self.key_id):
            _reference(value)
        _timestamp(self.authority_revision)
        _key_revision(self.key_revision)
        _timestamp(self.approval_expires_at_ms)


@dataclass(frozen=True, slots=True, repr=False)
class OriginalSessionSnapshot:
    """Owner response, not a generated EG DTO or durable lookup implementation."""

    record: SessionRecord
    scopes: tuple[str, ...]
    authority_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.record, SessionRecord):
            raise ValueError("durable session facts required")
        _timestamp(self.authority_revision)
        object.__setattr__(self, "scopes", _strings(self.scopes, scopes=True))


@dataclass(frozen=True, slots=True, repr=False)
class RegisteredApprovalKey:
    """Attested owner snapshot; possession of public key bytes proves no owner."""

    key_id: str
    tenant: str
    principal_id: str
    revision: int
    public_key_hex: str
    expires_at_ms: int
    revoked_at_ms: int | None
    purpose: Literal["signed_approval"]

    def __post_init__(self) -> None:
        for value in (self.key_id, self.tenant, self.principal_id):
            _reference(value)
        _key_revision(self.revision)
        _timestamp(self.expires_at_ms)
        if self.revoked_at_ms is not None:
            _timestamp(self.revoked_at_ms)
        if type(self.public_key_hex) is not str or not re.fullmatch(
            "[0-9a-f]{64}", self.public_key_hex
        ):
            raise ValueError("Ed25519 public key lowercase hex required")
        if self.purpose != "signed_approval":
            raise ValueError("approval-purpose key required")


class OriginalSessionResolver(Protocol):
    async def resolve_original_session(self, reference: str) -> OriginalSessionSnapshot:
        """Inspect existing durable facts without touching expiry or issuing authority.

        The owner refuses absent, expired, revoked or foreign references.
        A global authority revision may change for unrelated identity writes.
        """
        ...


class HumanApprovalKeyAuthority(Protocol):
    async def resolve_key(
        self, key_id: str, *, principal_id: str, purpose: Literal["signed_approval"]
    ) -> RegisteredApprovalKey:
        """Resolve current registered human ownership, purpose, validity and revision."""
        ...


class HumanSnapshotCheck(StrEnum):
    CURRENT_SNAPSHOT_ONLY = "CURRENT_SNAPSHOT_ONLY"
    AUTHORITY_UNAVAILABLE = "AUTHORITY_UNAVAILABLE"
    AUTHORITY_REFUSED = "AUTHORITY_REFUSED"
    BINDING_MISMATCH = "BINDING_MISMATCH"
    EXPIRED = "EXPIRED"
    SNAPSHOT_CHANGED = "SNAPSHOT_CHANGED"


def _check(
    binding: OriginalHumanBinding,
    current: SessionState,
    session: OriginalSessionSnapshot,
    key: RegisteredApprovalKey,
    now_ms: int,
) -> HumanSnapshotCheck | None:
    record, resolution = session.record, current.resolution
    if key.revoked_at_ms is not None:
        return HumanSnapshotCheck.AUTHORITY_REFUSED
    if resolution.kind != "human" or resolution.is_bootstrap:
        return HumanSnapshotCheck.AUTHORITY_REFUSED
    actual = (
        current.session_ref,
        record.session_ref,
        record.tenant,
        record.principal_id,
        frozenset(session.scopes),
        resolution.request_context["tenant"],
        resolution.principal_id,
        session.authority_revision,
        key.key_id,
        key.revision,
        key.tenant,
        key.principal_id,
    )
    expected = (
        binding.session_ref,
        binding.session_ref,
        binding.tenant,
        binding.human,
        frozenset(resolution.scopes),
        binding.tenant,
        binding.human,
        binding.authority_revision,
        binding.key_id,
        binding.key_revision,
        binding.tenant,
        binding.human,
    )
    if actual != expected:
        return HumanSnapshotCheck.BINDING_MISMATCH
    if now_ms >= min(
        key.expires_at_ms,
        current.expires_at_ms,
        binding.approval_expires_at_ms,
        record.absolute_expires_at_ms,
        record.idle_expires_at_ms,
    ):
        return HumanSnapshotCheck.EXPIRED
    return None


async def evaluate_human_snapshot(
    binding: OriginalHumanBinding,
    *,
    credential: str,
    sessions: SessionAuthority | None,
    references: OriginalSessionResolver | None,
    keys: HumanApprovalKeyAuthority | None,
    clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> HumanSnapshotCheck:
    """Re-read supplied owners without retaining authority or extending approval.

    Success describes these reads only. Consumers still require the native
    pending/plan/grant transaction and a qualified served authority composition.
    """
    if sessions is None or references is None or keys is None:
        return HumanSnapshotCheck.AUTHORITY_UNAVAILABLE
    previous: tuple[OriginalSessionSnapshot, RegisteredApprovalKey] | None = None
    try:
        async with asyncio.timeout(1):
            current_session = await sessions.resolve_session(credential)
            if not isinstance(current_session, SessionState):
                return HumanSnapshotCheck.AUTHORITY_UNAVAILABLE
            for _ in range(2):
                session = await references.resolve_original_session(binding.session_ref)
                key = await keys.resolve_key(
                    binding.key_id,
                    principal_id=binding.human,
                    purpose="signed_approval",
                )
                if not isinstance(session, OriginalSessionSnapshot) or not isinstance(
                    key, RegisteredApprovalKey
                ):
                    return HumanSnapshotCheck.AUTHORITY_UNAVAILABLE
                now_ms = clock_ms()
                _timestamp(now_ms)
                refusal = _check(binding, current_session, session, key, now_ms)
                if refusal is not None:
                    return refusal
                current = (session, key)
                if previous is not None and current != previous:
                    return HumanSnapshotCheck.SNAPSHOT_CHANGED
                previous = current
    except PermissionError:
        return HumanSnapshotCheck.AUTHORITY_REFUSED
    except (IdentityUnavailable, TimeoutError):
        return HumanSnapshotCheck.AUTHORITY_UNAVAILABLE
    return HumanSnapshotCheck.CURRENT_SNAPSHOT_ONLY
