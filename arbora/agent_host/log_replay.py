"""Bounded replay of the existing private rotating files, with scoped cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
from collections.abc import Iterator

from arbora.agent_host.log_access import (
    HostLogDenied,
    HostLogRecord,
    valid_generations,
)


def private_file(directory: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise HostLogDenied("log_unavailable")
    return fd


def cursor_key(directory: int) -> bytes:
    try:
        fd = private_file(
            directory, ".cursor-key", os.O_WRONLY | os.O_CREAT | os.O_EXCL
        )
    except FileExistsError as error:
        fd = private_file(directory, ".cursor-key", os.O_RDONLY)
        try:
            key = os.read(fd, 33)
        finally:
            os.close(fd)
        if len(key) != 32:
            raise HostLogDenied("log_unavailable") from error
        return key
    try:
        key = secrets.token_bytes(32)
        if os.write(fd, key) != len(key):
            raise OSError("log_unavailable")
        os.fsync(fd)
        os.fsync(directory)
        return key
    finally:
        os.close(fd)


def retained_records(
    directory: int, workspace: str
) -> Iterator[tuple[HostLogRecord, str]]:
    sequences: dict[tuple[str, str], int] = {}
    for name in (
        "server.log.4",
        "server.log.3",
        "server.log.2",
        "server.log.1",
        "server.log",
    ):
        try:
            fd = private_file(directory, name, os.O_RDONLY)
        except FileNotFoundError:
            continue
        with os.fdopen(fd, "rb") as source:
            if os.fstat(source.fileno()).st_size > 10 * 1024 * 1024:
                raise HostLogDenied("log_unavailable")
            while line := source.readline(16385):
                if len(line) > 16384 or not line.endswith(b"\n"):
                    raise HostLogDenied("log_unavailable")
                record = decode_record(line, workspace)
                scope = (record.tenant, record.host_instance)
                previous = sequences.get(scope)
                if previous is not None and record.sequence != previous + 1:
                    raise HostLogDenied("log_unavailable")
                if previous is None and len(sequences) >= 4096:
                    raise HostLogDenied("log_unavailable")
                sequences[scope] = record.sequence
                yield record, hashlib.sha256(line).hexdigest()


def decode_record(line: bytes, workspace: str) -> HostLogRecord:
    try:
        value = json.loads(line)
        if not isinstance(value, dict) or set(value) != {
            "tenant",
            "workspace",
            "host_instance",
            "request",
            "sequence",
            "timestamp",
            "severity",
            "message",
        }:
            raise ValueError
        if value["workspace"] != workspace or not valid_generations(
            (value["host_instance"],)
        ):
            raise ValueError
        if any(
            type(value[field]) is not str
            for field in ("tenant", "request", "severity", "message")
        ):
            raise ValueError
        if (
            not value["tenant"]
            or type(value["sequence"]) is not int
            or not 1 <= value["sequence"] <= 2**63 - 1
        ):
            raise ValueError
        if type(value["timestamp"]) not in (int, float) or not math.isfinite(
            value["timestamp"]
        ):
            raise ValueError
        return HostLogRecord(
            value["tenant"],
            workspace,
            value["host_instance"],
            value["message"],
            value["request"],
            value["sequence"],
            value["timestamp"],
            value["severity"],
        )
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError) as error:
        raise HostLogDenied("log_unavailable") from error


class ReplayCursor:
    """Integrity only: current session and workspace grants remain mandatory."""

    def __init__(
        self, key: bytes, workspace: str, tenant: str, generations: tuple[str, ...]
    ) -> None:
        self._key = key
        self._scope = hashlib.sha256(
            json.dumps([workspace, tenant, generations], separators=(",", ":")).encode()
        ).hexdigest()

    def encode(self, anchor: str) -> str:
        payload = f"v1:{self._scope}:{anchor}".encode()
        return base64.urlsafe_b64encode(
            payload + hmac.digest(self._key, payload, "sha256")
        ).decode()

    def decode(self, cursor: str) -> str:
        try:
            raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(
                signature, hmac.digest(self._key, payload, "sha256")
            ):
                raise ValueError
            version, scope, anchor = payload.decode().split(":")
            if version != "v1" or scope != self._scope or len(anchor) != 64:
                raise ValueError
            return anchor
        except (ValueError, UnicodeError) as error:
            raise HostLogDenied("workspace_denied") from error
