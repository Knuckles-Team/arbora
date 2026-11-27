"""The lock backup store: where a sync keeps the lock file it can roll back to (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arbora.deployment.venv_plan import (
    VenvSyncError,
    Workspace,
    redact_path_for_log,
)

from .venv_sync_types import (
    Backup,
)

logger = logging.getLogger(__name__)


class BackupNotFoundError(VenvSyncError):
    """The requested lock backup id does not exist."""


# ─────────────────────────────────────────────────────────────────────────────
# Lock backups — the only rollback path an untracked lock has
# ─────────────────────────────────────────────────────────────────────────────
class LockBackupStore:
    """Content-addressed archive of ``uv.lock`` revisions.

    ``uv.lock`` is untracked and lives at a non-git root, so there is no other
    way back.  Today's only successful rollback happened because somebody
    manually copied the file to a scratch directory first; this makes that
    automatic and unconditional.
    """

    def __init__(self, workspace: Workspace, *, retain: int = 50) -> None:
        self.workspace = workspace
        self.retain = retain
        self.directory = workspace.state_dir / "lock-backups"

    def create(self, reason: str, *, meta: dict[str, Any] | None = None) -> Backup:
        source = self.workspace.lock
        if not source.is_file():
            raise VenvSyncError(f"no lock to back up at {source}")
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"{stamp}-{digest[:12]}"
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{backup_id}.lock"
        if not target.exists():
            _atomic_write_bytes(target, payload)
        backup = Backup(
            id=backup_id,
            path=target,
            created_at=datetime.now(UTC).isoformat(),
            digest=digest,
            reason=reason,
            meta=meta or {},
        )
        _atomic_write_text(
            self.directory / f"{backup_id}.json",
            json.dumps(backup.as_dict(), indent=2, sort_keys=True),
        )
        self._prune()
        logger.info("lock backup %s created (%s)", backup_id, reason)
        return backup

    def list(self) -> list[Backup]:
        if not self.directory.is_dir():
            return []
        backups: list[Backup] = []
        for meta_path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    "ignoring unreadable backup record %s: %s", meta_path, exc
                )
                continue
            backups.append(
                Backup(
                    id=str(payload.get("id", meta_path.stem)),
                    path=Path(payload.get("path", meta_path.with_suffix(".lock"))),
                    created_at=str(payload.get("created_at", "")),
                    digest=str(payload.get("digest", "")),
                    reason=str(payload.get("reason", "")),
                    verified=bool(payload.get("verified", False)),
                    meta=payload.get("meta") or {},
                )
            )
        return sorted(backups, key=lambda b: b.id)

    def get(self, backup_id: str) -> Backup:
        for backup in self.list():
            if backup.id == backup_id:
                return backup
        raise BackupNotFoundError(f"no lock backup with id {backup_id!r}")

    def latest(self) -> Backup | None:
        backups = self.list()
        return backups[-1] if backups else None

    def restore(self, backup_id: str | None = None) -> Backup:
        backup = self.get(backup_id) if backup_id else self.latest()
        if backup is None:
            raise BackupNotFoundError("no lock backups exist to restore from")
        payload = backup.path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != backup.digest:
            raise VenvSyncError(
                f"backup {backup.id} failed its own digest check; refusing to restore"
            )
        _atomic_write_bytes(self.workspace.lock, payload)
        logger.info("restored uv.lock from backup %s", backup.id)
        return backup

    def mark_verified(self, backup_id: str) -> Backup:
        backup = self.get(backup_id)
        verified = replace(backup, verified=True)
        _atomic_write_text(
            self.directory / f"{backup_id}.json",
            json.dumps(verified.as_dict(), indent=2, sort_keys=True),
        )
        return verified

    @contextmanager
    def checkpoint(
        self, reason: str, *, meta: dict[str, Any] | None = None
    ) -> Iterator[Backup]:
        """Back the lock up, then restore it if the block raises."""

        backup = self.create(reason, meta=meta)
        try:
            yield backup
        except BaseException as exc:
            self.restore(backup.id)
            raise VenvSyncError(
                f"restored uv.lock from checkpoint {backup.id} after a failed "
                f"{reason}: {exc}"
            ) from exc

    def _prune(self) -> None:
        backups = self.list()
        # Verified backups are the known-good rollback targets; retention only
        # ever discards unverified ones.
        removable = [b for b in backups if not b.verified]
        excess = len(backups) - self.retain
        for backup in removable[: max(0, excess)]:
            _unlink_quietly(backup.path)
            _unlink_quietly(self.directory / f"{backup.id}.json")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, payload: str) -> None:
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove %s: %s", redact_path_for_log(path), exc)
