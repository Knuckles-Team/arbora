"""Scheduled directory sync apply (ARBORA-IDENTITY-R010.2.3.2).

The job ``identity.directory_sync`` runs every ``identity.ldap.sync_interval_seconds``
(default 900, range [60, 86400]). One run binds the directory, plans from the snapshot with
:func:`~arbora.identity.ldap.plan_directory_sync` and applies create, update, then
deprovision through the identity port. A bind failure skips the run and keeps the prior
state; a plan that deprovisions more than ``identity.ldap.max_deprovision_fraction``
(default 0.2, range [0, 1]) refuses before any write. The plan carries each user's full
intended role set, because the engine replaces a role set. Arbora keeps no account store.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from arbora.identity.engine import IdentityUnavailable
from arbora.identity.ldap import (
    DirectoryUser,
    LdapBindConfig,
    bind_directory,
    plan_directory_sync,
)

__all__ = [
    "JOB_ID",
    "SyncConfig",
    "SyncRefused",
    "SyncReport",
    "directory_sync_config",
    "run_sync",
]

JOB_ID = "identity.directory_sync"
INTERVAL_RANGE = (60, 86_400)
FRACTION_RANGE = (0.0, 1.0)
_PAGE = 500


class SyncRefused(Exception):
    """A stable code; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Directory(Protocol):
    def bind(self, config: LdapBindConfig) -> None: ...

    def snapshot(self) -> Collection[DirectoryUser]: ...


@dataclass(frozen=True, slots=True)
class SyncConfig:
    bind: LdapBindConfig
    mapping: Mapping[str, Collection[str]]
    interval_seconds: int = 900
    max_deprovision_fraction: float = 0.2

    def __post_init__(self) -> None:
        low, high = INTERVAL_RANGE
        if (
            type(self.interval_seconds) is not int
            or not low <= self.interval_seconds <= high
        ):
            raise ValueError("identity.ldap.sync_interval_seconds is out of range")
        if not FRACTION_RANGE[0] <= self.max_deprovision_fraction <= FRACTION_RANGE[1]:
            raise ValueError("identity.ldap.max_deprovision_fraction is out of range")


@dataclass(frozen=True, slots=True)
class SyncReport:
    created: int
    updated: int
    deprovisioned: int


def directory_sync_config(
    document: Mapping[str, Any],
    bind: LdapBindConfig,
    mapping: Mapping[str, Collection[str]],
) -> SyncConfig:
    block = (document.get("identity") or {}).get("ldap") or {}
    return SyncConfig(
        bind=bind,
        mapping=mapping,
        interval_seconds=int(block.get("sync_interval_seconds", 900)),
        max_deprovision_fraction=float(block.get("max_deprovision_fraction", 0.2)),
    )


async def _existing(
    port: Any, client: Any, claims: Mapping[str, Any]
) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    after = None
    while True:
        page = await port.identity(
            client,
            claims,
            "user",
            "list",
            {"after": after, "limit": _PAGE},
            expect="users",
        )
        rows.update({row["username"]: row for row in page})
        if len(page) < _PAGE:
            return rows
        after = page[-1]["principal_id"]


async def run_sync(
    port: Any,
    client: Any,
    claims: Mapping[str, Any],
    directory: Directory,
    config: SyncConfig,
    *,
    emit: Callable[[Mapping[str, str]], None] = lambda event: None,
) -> SyncReport:
    """One sync run: bind, plan, then create, update and deprovision."""
    try:
        bind_directory(directory, config.bind)
    except IdentityUnavailable:
        emit({"op": JOB_ID, "outcome": "DIRECTORY_BIND_FAILED"})
        raise SyncRefused("DIRECTORY_BIND_FAILED") from None
    snapshot = directory.snapshot()
    plan = plan_directory_sync(snapshot, config.mapping)
    if len(plan.deprovision) / len(snapshot) > config.max_deprovision_fraction:
        emit({"op": JOB_ID, "outcome": "DEPROVISION_FRACTION_EXCEEDED"})
        raise SyncRefused("DEPROVISION_FRACTION_EXCEEDED")
    known = await _existing(port, client, claims)
    created = updated = deprovisioned = 0
    for username, roles in sorted(plan.assignments.items()):
        wanted = sorted(roles)
        if username not in known:
            await port.identity(
                client,
                claims,
                "user",
                "create",
                {"username": username, "roles": wanted, "kind": "human"},
                expect="principal",
            )
            created += 1
        elif sorted(known[username].get("roles", ())) != wanted:
            principal_id = known[username]["principal_id"]
            for role in wanted:
                await port.identity(
                    client,
                    claims,
                    "access",
                    "change_user_role",
                    {"principal_id": principal_id, "role_id": role, "change": "assign"},
                    expect="done",
                )
            for role in set(known[username].get("roles", ())) - set(wanted):
                await port.identity(
                    client,
                    claims,
                    "access",
                    "change_user_role",
                    {"principal_id": principal_id, "role_id": role, "change": "remove"},
                    expect="done",
                )
            updated += 1
    for username in plan.deprovision:
        row = known.get(username)
        if row is None:
            continue
        await port.identity(
            client,
            claims,
            "user",
            "set_status",
            {"principal_id": row["principal_id"], "status": "disabled"},
            expect="done",
        )
        await port.identity(
            client,
            claims,
            "session",
            "revoke_all",
            {"id": row["principal_id"]},
            expect="done",
        )
        deprovisioned += 1
    emit(
        {
            "op": JOB_ID,
            "outcome": "ok",
            "created": str(created),
            "updated": str(updated),
            "deprovisioned": str(deprovisioned),
        }
    )
    return SyncReport(created, updated, deprovisioned)
