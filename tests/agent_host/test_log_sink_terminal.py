"""Native Tuig served receipt: Arbora sink, HTTP adapter and terminal reader.

This is the source-composition proof for the host-log contract.  The former
Python/Textual package is retired; the test therefore launches the reviewed
native Tuig executable against the real loopback Arbora app assembled by the
existing host fixtures.  Deployment through a central MCP or gateway is a
separate qualification.
"""

# spec: ARBORA-HOST-R009.8.2.2

from __future__ import annotations

import asyncio
import codecs
import fcntl
import os
import pty
import select
import shutil
import struct
import subprocess
import termios
import time
from pathlib import Path

import pytest

from tests.agent_host.test_log_access import ControlledOwners, serve
from tests.agent_host.test_log_sink import runtime_for

_RECEIPT_FIELDS = frozenset(
    {
        "version",
        "log_ref",
        "host_instance",
        "endpoint",
        "workspace",
        "tenant",
        "principal",
        "session_ref",
        "policy_revision",
        "grant_revision",
        "expires_at_ms",
        "interface_mode",
        "web_logs",
        "ready",
        "source_generations",
    }
)


def _tuig_binary() -> Path:
    configured = os.environ.get("TUIG_BIN")
    candidate = Path(configured) if configured else shutil.which("tuig")
    if candidate is None:
        raise AssertionError(
            "a reviewed native Tuig executable is required; install the published "
            "native artifact or set TUIG_BIN when running against a reviewed build"
        )
    path = Path(candidate)
    if not path.is_file() or not os.access(path, os.X_OK):
        pytest.fail(f"TUIG_BIN is not an executable file: {path}")
    help_result = subprocess.run(
        [str(path), "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "Tuig: native Arbora terminal" in help_result.stdout
    return path


class _TerminalScreen:
    """Small deterministic ANSI screen model for the native PTY receipt."""

    def __init__(self, rows: int = 80, columns: int = 200) -> None:
        self.rows = rows
        self.columns = columns
        self.cells: list[list[str]] = [[" "] * columns for _ in range(rows)]
        self.row = 0
        self.column = 0
        self.saved: tuple[int, int] = (0, 0)
        self._csi: str | None = None
        self._osc = False

    def _ensure_row(self, row: int) -> None:
        while row >= len(self.cells):
            self.cells.append([" "] * self.columns)

    def _clear_line(self, start: int = 0, end: int | None = None) -> None:
        self._ensure_row(self.row)
        end = self.columns if end is None else min(self.columns, end)
        for index in range(max(0, start), end):
            self.cells[self.row][index] = " "

    def _clear_screen(self) -> None:
        self.cells = [[" "] * self.columns for _ in range(self.rows)]

    @staticmethod
    def _parameters(value: str) -> list[int]:
        parts = value.lstrip("?").split(";", 1)
        values: list[int] = []
        for item in parts:
            try:
                values.append(int(item or "0"))
            except ValueError:
                values.append(0)
        return values

    def _csi_command(self, value: str, command: str) -> None:
        params = self._parameters(value)
        first = params[0] if params else 0
        if command in {"H", "f"}:
            self.row = max(0, (params[0] if params and params[0] else 1) - 1)
            self.column = max(
                0, (params[1] if len(params) > 1 and params[1] else 1) - 1
            )
            self._ensure_row(self.row)
        elif command == "A":
            self.row = max(0, self.row - (first or 1))
        elif command == "B":
            self.row += first or 1
            self._ensure_row(self.row)
        elif command == "C":
            self.column += first or 1
        elif command == "D":
            self.column = max(0, self.column - (first or 1))
        elif command == "G":
            self.column = max(0, (first or 1) - 1)
        elif command == "d":
            self.row = max(0, (first or 1) - 1)
            self._ensure_row(self.row)
        elif command == "J":
            if first in {2, 3}:
                self._clear_screen()
            elif first == 0:
                self._clear_line(self.column)
                for row in range(self.row + 1, len(self.cells)):
                    self.cells[row] = [" "] * self.columns
        elif command == "K":
            if first == 1:
                self._clear_line(0, self.column + 1)
            elif first == 2:
                self._clear_line()
            else:
                self._clear_line(self.column)
        elif command == "P":
            count = first or 1
            line = self.cells[self.row]
            del line[self.column : self.column + count]
            line.extend([" "] * min(count, self.columns))
        elif command == "@":
            count = first or 1
            line = self.cells[self.row]
            line[self.column : self.column] = [" "] * count
            del line[self.columns :]
        elif command == "X":
            self._clear_line(self.column, self.column + (first or 1))
        elif command == "s":
            self.saved = (self.row, self.column)
        elif command == "u":
            self.row, self.column = self.saved

    def _put(self, character: str) -> None:
        self._ensure_row(self.row)
        if self.column >= self.columns:
            self.column = 0
            self.row += 1
            self._ensure_row(self.row)
        self.cells[self.row][self.column] = character
        self.column += 1

    def feed(self, text: str) -> None:
        for character in text:
            if self._osc:
                if character == "\a":
                    self._osc = False
                continue
            if self._csi is not None:
                if self._csi == "\x1b":
                    if character == "[":
                        self._csi = "\x1b["
                    elif character == "]":
                        self._csi = None
                        self._osc = True
                    elif character == "7":
                        self.saved = (self.row, self.column)
                        self._csi = None
                    elif character == "8":
                        self.row, self.column = self.saved
                        self._csi = None
                    elif character == "c":
                        self._clear_screen()
                        self.row = self.column = 0
                        self._csi = None
                    else:
                        self._csi = None
                elif "@" <= character <= "~":
                    value, self._csi = self._csi[2:], None
                    self._csi_command(value, character)
                else:
                    self._csi += character
                continue
            if character == "\x1b":
                self._csi = "\x1b"
            elif character == "\r":
                self.column = 0
            elif character == "\n":
                self.row += 1
                self._ensure_row(self.row)
            elif character == "\b":
                self.column = max(0, self.column - 1)
            elif character == "\t":
                self.column = min(self.columns - 1, ((self.column // 8) + 1) * 8)
            elif character == "\x07":
                continue
            elif ord(character) >= 0x20:
                self._put(character)

    def snapshot(self) -> str:
        return "\n".join("".join(row).rstrip() for row in self.cells).rstrip()


def _contains_terminal_phrase(screen: str, phrase: str) -> bool:
    """Allow terminal word wrapping while retaining the complete phrase."""

    return " ".join(phrase.split()) in " ".join(screen.split())


def _read_native_output(
    binary: Path, endpoint: str, expected: str, *, timeout: float = 12
) -> str:
    """Run Tuig in a real PTY and wait for the rendered authenticated log line."""

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    environment = os.environ.copy()
    environment.update(
        {
            "AGENT_URL": endpoint,
            "AGENT_BEARER_TOKEN": "a",
            "AGENT_AUTH_MODE": "bearer",
            "AGENT_TASK_ID": "fixture-task",
            "AGENT_SUBMISSION_TIMEOUT_SECONDS": "1",
            "AGENT_INCLUDE_COMMITTED_EVENT": "false",
            "NO_COLOR": "1",
            "TERM": "xterm-256color",
        }
    )
    process = subprocess.Popen(
        [str(binary)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env=environment,
        start_new_session=True,
    )
    os.close(slave)

    decoder = codecs.getincrementaldecoder("utf-8")()
    screen = _TerminalScreen()
    seen_screen = ""
    started = time.monotonic()
    sent_logs_key = False
    sent_quit = False
    try:
        while time.monotonic() - started < timeout:
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                try:
                    chunk = os.read(master, 65_536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                screen.feed(decoder.decode(chunk))

            # AGENT_TASK_ID keeps the native UI out of the composer.  Once the
            # initial task request has had a bounded opportunity to settle,
            # open the real host-log view through its normal key route.
            if not sent_logs_key and time.monotonic() - started >= 2:
                os.write(master, b"l")
                sent_logs_key = True

            rendered = screen.snapshot()
            if _contains_terminal_phrase(rendered, expected) and not sent_quit:
                seen_screen = rendered
                os.write(master, b"q")
                sent_quit = True
                break
            if process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            if not sent_quit:
                try:
                    os.write(master, b"q")
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        os.close(master)

    screen.feed(decoder.decode(b"", final=True))
    rendered = screen.snapshot()
    if not seen_screen:
        seen_screen = rendered
    assert process.returncode == 0, seen_screen[-4000:]
    assert _contains_terminal_phrase(seen_screen, expected), seen_screen[-4000:]
    return seen_screen


def test_native_tuig_binds_reads_and_refuses_stale_or_spoofed_receipts(
    tmp_path: Path,
) -> None:
    """Prove native bind/read plus authority, origin and generation refusal."""

    binary = _tuig_binary()
    owners = ControlledOwners()
    runtime = runtime_for(owners, tmp_path, True)
    # Warm the real sink's privacy serializer before the middleware's one
    # second admission deadline.  This remains a bounded fixture workspace;
    # production request admission still goes through HostLogRuntime.
    asyncio.run(
        runtime.sink.emit(
            tenant="tenant-a",
            request_id="fixture-preflight",
            message="fixture preflight",
        )
    )
    try:
        with serve(None, runtime) as client:
            assert (
                client.get("/health", headers={"Authorization": "Bearer a"}).status_code
                == 200
            )
            response = client.post(
                "/host/logs/reference", headers={"Authorization": "Bearer a"}
            )
            assert response.status_code == 200, response.text
            receipt = response.json()
            assert set(receipt) == _RECEIPT_FIELDS
            assert receipt["version"] == 1
            assert receipt["ready"] is True
            assert receipt["web_logs"] is True
            assert receipt["interface_mode"] == "terminal"
            assert receipt["endpoint"] == f"{client.base_url!s}".rstrip("/") + "/"
            assert receipt["source_generations"] == [runtime.sink.host_instance]

            log_ref = receipt["log_ref"]
            read_response = client.post(
                "/host/logs/read",
                headers={"Authorization": "Bearer a"},
                json={"version": 2, "log_ref": log_ref, "cursor": None, "limit": 128},
            )
            assert read_response.status_code == 200, read_response.text
            assert "Authenticated host request received" in read_response.text

            rendered = _read_native_output(
                binary,
                f"{client.base_url!s}".rstrip("/") + "/",
                "Authenticated host request received",
            )
            assert "Host logs" in rendered

            # A grant revision/source generation change invalidates the bound
            # handle before any source bytes are returned.
            owners.change["source_generations"] = ("rotated-host",)
            assert (
                client.post(
                    "/host/logs/read",
                    headers={"Authorization": "Bearer a"},
                    json={
                        "version": 2,
                        "log_ref": log_ref,
                        "cursor": None,
                        "limit": 128,
                    },
                ).status_code
                == 403
            )

            # Host and forwarded authorities are not caller-controlled receipt
            # origins; both refusal paths are kept explicit here alongside the
            # reusable host-access tests.
            spoof = client.post(
                "/host/logs/reference",
                headers={
                    "Authorization": "Bearer a",
                    "Host": "untrusted.example",
                },
            )
            assert spoof.status_code == 403
            forwarded = client.post(
                "/host/logs/reference",
                headers={
                    "Authorization": "Bearer a",
                    "X-Forwarded-Host": "untrusted.example",
                },
            )
            assert forwarded.status_code == 403

            # A missing/forged reader reference is refused without invoking a
            # source read, matching the native adapter's missing-reader fence.
            forged = client.post(
                "/host/logs/read",
                headers={"Authorization": "Bearer a"},
                json={
                    "version": 2,
                    "log_ref": "forged-reader",
                    "cursor": None,
                    "limit": 128,
                },
            )
            assert forged.status_code == 403

            owners.allowed = False
            assert (
                client.post(
                    "/host/logs/read",
                    headers={"Authorization": "Bearer a"},
                    json={
                        "version": 2,
                        "log_ref": log_ref,
                        "cursor": None,
                        "limit": 128,
                    },
                ).status_code
                == 403
            )
    finally:
        runtime.sink.close()
