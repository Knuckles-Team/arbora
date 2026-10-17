"""Owner-only rotating host sink with bounded private tenant projections."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import stat
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from graph_os.agent_host.log_access import (
    HostLogBatch,
    HostLogDenied,
    HostLogRecord,
    valid_generations,
)


class WorkspaceLogSink:
    """Write before projection; never hand a shared descriptor to a client.

    Projections and opaque cursors are host-lifetime state, not durable replay.
    A restarted host must issue new references and a new host instance binding.
    """

    def __init__(
        self, workspace: str, host_instance: str, *, max_bytes: int = 10 * 1024 * 1024
    ) -> None:
        if not host_instance or not 1024 <= max_bytes <= 10 * 1024 * 1024:
            raise ValueError("invalid sink bounds")
        self.workspace = str(Path(workspace).resolve(strict=True))
        self.host_instance = host_instance
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._io_slots = threading.BoundedSemaphore(1)
        self._pending: set[asyncio.Task] = set()
        self._records: dict[str, deque[tuple[int, HostLogRecord]]] = {}
        self._sequences: dict[str, int] = {}
        self._cursors: OrderedDict[str, tuple[str, tuple[str, ...], int]] = (
            OrderedDict()
        )
        self._closed = False
        self._directory = self._open_directory()
        try:
            fcntl.flock(self._directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd = self._file("server.log")
            os.close(fd)
        except BaseException:
            os.close(self._directory)
            raise

    def _open_directory(self) -> int:
        fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in ("agent_data", "logs"):
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    raise PermissionError("log_unavailable")
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("log_unavailable")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _file(self, name: str) -> int:
        fd = os.open(
            name,
            os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=self._directory,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            os.close(fd)
            raise PermissionError("log_unavailable")
        return fd

    def _rotate(self) -> None:
        for index in range(4, 0, -1):
            source = "server.log" if index == 1 else f"server.log.{index - 1}"
            target = f"server.log.{index}"
            try:
                info = os.stat(source, dir_fd=self._directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise PermissionError("log_unavailable")
            os.replace(
                source, target, src_dir_fd=self._directory, dst_dir_fd=self._directory
            )

    async def emit(self, *, tenant: str, request_id: str, message: str) -> None:
        """Redact before any bytes enter the required file or private projection."""
        await self._submit(self._emit, tenant, request_id, message)

    async def _submit(self, action: Callable[..., Any], *args: Any) -> Any:
        if not self._io_slots.acquire(blocking=False):
            raise HostLogDenied("log_unavailable")
        worker = asyncio.create_task(asyncio.to_thread(self._reserved, action, *args))
        self._pending.add(worker)
        worker.add_done_callback(self._completed)
        return await asyncio.shield(worker)

    def _completed(self, worker: asyncio.Task) -> None:
        self._pending.discard(worker)
        if not worker.cancelled():
            worker.exception()

    def _reserved(self, action: Callable[..., Any], *args: Any) -> Any:
        try:
            return action(*args)
        finally:
            self._io_slots.release()

    def _emit(self, tenant: str, request_id: str, message: str) -> None:
        from agent_utilities.security.persistence_privacy import (
            sanitize_for_persistence,
        )

        clean, _ = sanitize_for_persistence(
            {"request": request_id[:128], "message": message[:16384]}
        )
        safe_message = str(clean["message"])
        with self._lock:
            if self._closed or (
                tenant not in self._records and len(self._records) >= 64
            ):
                raise HostLogDenied("log_unavailable")
            sequence = self._sequences.get(tenant, 0) + 1
            record = {
                "tenant": tenant,
                "workspace": self.workspace,
                "host_instance": self.host_instance,
                "request": clean["request"],
                "sequence": sequence,
                "timestamp": time.time(),
                "severity": "INFO",
                "message": safe_message,
            }
            data = (json.dumps(record, ensure_ascii=True) + "\n").encode()
            if len(data) > min(16384, self._max_bytes):
                raise HostLogDenied("log_unavailable")
            fd = self._file("server.log")
            try:
                if os.fstat(fd).st_size + len(data) > self._max_bytes:
                    os.close(fd)
                    fd = -1
                    self._rotate()
                    fd = self._file("server.log")
                remaining = memoryview(data)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("log_unavailable")
                    remaining = remaining[written:]
            finally:
                if fd >= 0:
                    os.close(fd)
            self._sequences[tenant] = sequence
            projection = self._records.setdefault(tenant, deque(maxlen=128))
            projection.append(
                (
                    sequence,
                    HostLogRecord(
                        tenant, self.workspace, self.host_instance, safe_message
                    ),
                )
            )

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
        return await self._submit(
            self._read,
            tenant,
            workspace,
            host_instance,
            source_generations,
            cursor,
            limit,
        )

    def _read(
        self,
        tenant: str,
        workspace: str,
        host_instance: str,
        source_generations: tuple[str, ...],
        cursor: str | None,
        limit: int,
    ) -> HostLogBatch:
        with self._lock:
            if (
                self._closed
                or not valid_generations(source_generations)
                or (workspace, host_instance) != (self.workspace, self.host_instance)
                or not 1 <= limit <= 128
            ):
                raise HostLogDenied("log_unavailable")
            after = 0
            if cursor is not None:
                binding = self._cursors.get(cursor)
                if binding is None or binding[:2] != (tenant, source_generations):
                    raise HostLogDenied("log_unavailable")
                after = binding[2]
            rows = self._records.get(tenant, ())
            gap = bool(rows and after < rows[0][0] - 1)
            selected = [
                (sequence, record)
                for sequence, record in rows
                if sequence > after and record.host_instance in source_generations
            ][:limit]
            next_sequence = selected[-1][0] if selected else after
            opaque = secrets.token_urlsafe(24)
            self._cursors[opaque] = (tenant, source_generations, next_sequence)
            if len(self._cursors) > 4096:
                self._cursors.popitem(last=False)
            return HostLogBatch(tuple(record for _, record in selected), opaque, gap)

    def close(self) -> None:
        """Close only this sink's descriptor; borrowed callers must not close it."""
        if not self._lock.acquire(timeout=5):
            raise RuntimeError("shutdown_incomplete")
        try:
            if not self._closed:
                os.close(self._directory)
                self._closed = True
                self._records.clear()
                self._cursors.clear()
        finally:
            self._lock.release()
