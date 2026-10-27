"""Bounded numeric evidence for fixture-owned children; no process payloads."""

from __future__ import annotations

import contextlib
import json
import math
import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any


def _read(path: Path) -> str:
    try:
        with path.open("r", encoding="ascii", errors="replace") as stream:
            return stream.read(8192)
    except OSError:
        return ""


def _numbers(path: Path, keys: set[str]) -> dict[str, int]:
    result = {}
    for line in _read(path).splitlines():
        fields = line.replace(":", " ").split()
        if (
            len(fields) >= 2
            and fields[0] in keys
            and len(fields[1]) <= 20
            and fields[1].isdigit()
        ):
            result[fields[0]] = int(fields[1])
    return result


def _pressure(path: Path) -> dict[str, float]:
    result = {}
    for line in _read(path).splitlines():
        fields = line.split()
        if not fields or fields[0] not in {"some", "full"}:
            continue
        for field in fields[1:]:
            key, _, value = field.partition("=")
            if key not in {"avg10", "avg60", "avg300", "total"}:
                continue
            try:
                number = float(value)
            except ValueError:
                continue
            if math.isfinite(number) and number >= 0:
                result[f"{fields[0]}_{key}"] = number
    return result


class BootDiagnostics:
    """Sample only on failure, before the existing fixture teardown runs."""

    def __init__(self, root: Path, processes: Sequence[Any]) -> None:
        self.root = root
        self.processes = processes

    def snapshot(self) -> dict[str, Any]:
        children = []
        roles = ("engine", "model", "connector", "ingest", "arbora")
        for role, process in zip(roles, self.processes, strict=False):
            proc = Path("/proc") / str(process.pid)
            status = _read(proc / "status")
            state = next(
                (
                    line.split()[1]
                    for line in status.splitlines()
                    if line.startswith("State:") and len(line.split()) > 1
                ),
                "",
            )
            children.append(
                {
                    "role": role,
                    "alive": process.poll() is None,
                    "state": state if state in set("RSDZTtXxKWPI") else "unknown",
                    "memory_kib": _numbers(proc / "status", {"VmRSS", "VmHWM"}),
                    "io_bytes": _numbers(proc / "io", {"read_bytes", "write_bytes"}),
                }
            )
        pressure = {
            kind: _pressure(Path("/proc/pressure") / kind)
            for kind in ("cpu", "memory", "io")
        }
        cgroup = Path("/sys/fs/cgroup")
        resources = {
            "cpu": _numbers(
                cgroup / "cpu.stat", {"usage_usec", "nr_throttled", "throttled_usec"}
            ),
            "memory_events": _numbers(
                cgroup / "memory.events", {"low", "high", "max", "oom", "oom_kill"}
            ),
        }
        disk = {}
        try:
            stat = os.statvfs(self.root)
            disk = {
                "available_bytes": stat.f_bavail * stat.f_frsize,
                "available_inodes": stat.f_favail,
            }
        except (OSError, AttributeError):
            pass
        return {
            "children": children,
            "pressure": pressure,
            "cgroup_mount_root": resources,
            "disk": disk,
        }

    @contextlib.contextmanager
    def phase(self, phase: str) -> Iterator[None]:
        if phase not in {"engine_socket", "engine_restart_socket", "http_operation"}:
            raise ValueError("Unsupported diagnostic phase")
        started = time.monotonic()
        try:
            yield
        except BaseException as error:
            try:
                evidence = {
                    "phase": phase,
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    **self.snapshot(),
                }
                error.add_note(
                    "boot_diagnostics=" + json.dumps(evidence, separators=(",", ":"))
                )
            except Exception:
                error.add_note("boot_diagnostics=unavailable")
            raise
