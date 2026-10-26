"""Drive the installed native Tuig executable, not a replacement consumer."""

import base64
import codecs
import hashlib
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.metadata import distribution
from pathlib import Path

import pyte

if sys.platform == "linux":
    import pty
    import termios


def installed_tuig() -> Path:
    """Bind acceptance to the locked distribution's recorded executable."""
    package = distribution("tuig")
    assert package.version == "2.3.6", "Install the locked native test extra"
    scripts = [
        entry
        for entry in package.files or ()
        if entry.name == "tuig" and entry.parent.name == "bin"
    ]
    assert len(scripts) == 1, "Native distribution must contain one tuig executable"
    record = scripts[0]
    path = Path(str(package.locate_file(record))).resolve(strict=True)
    payload = path.read_bytes()
    assert payload.startswith(b"\x7fELF") and os.access(path, os.X_OK)
    assert record.hash is not None and record.hash.mode == "sha256"
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    assert digest.decode() == record.hash.value, "Installed executable hash changed"
    return path


class NativeTerminal:
    """Read rendered terminal cells with finite deadlines and owned cleanup."""

    def __init__(self, endpoint: str, diagnostics: Callable[[], str]) -> None:
        self.diagnostics = diagnostics
        binary = installed_tuig()
        self.master, slave = pty.openpty()
        termios.tcsetwinsize(slave, (40, 150))
        self.original_terminal = termios.tcgetattr(slave)
        environment = {
            **os.environ,
            "TERM": "xterm-256color",
            "NO_COLOR": "1",
            "AGENT_URL": endpoint,
            "AGENT_AUTH_MODE": "bearer",
            "AGENT_BEARER_TOKEN": "a",
            "AGENT_TASK_ID": "unavailable-fixture-task",
            "AGENT_SUBMISSION_TIMEOUT_SECONDS": "300",
            "AGENT_INCLUDE_COMMITTED_EVENT": "false",
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
        }
        try:
            self.process = subprocess.Popen(
                [str(binary)], stdin=slave, stdout=slave, stderr=slave, env=environment
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.screen = pyte.Screen(150, 40)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def shown(self) -> str:
        return "\n".join(self.screen.display)

    def send(self, keys: bytes) -> None:
        assert os.write(self.master, keys) == len(keys)

    def wait_until(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            assert self.process.poll() is None, "Native terminal exited early"
            if select.select([self.master], [], [], 0.05)[0]:
                raw = os.read(self.master, 65536)
                assert raw, "Native terminal closed its PTY"
                if b"\x1b[6n" in raw:
                    self.send(b"\x1b[1;1R")
                self.stream.feed(self.decoder.decode(raw))
        raise AssertionError(
            f"Terminal expectation timed out:\n{self.shown()}"
            f"\nTransport metadata: {self.diagnostics()}"
        )

    def wait_for(self, text: str) -> str:
        self.wait_until(lambda: text in self.shown())
        return self.shown()

    def open_logs(self) -> None:
        self.wait_for("l: host logs")
        self.send(b"l")

    @contextmanager
    def stopped(self) -> Iterator[None]:
        """Pause polling without closing the native reader or replacing its cursor."""
        os.kill(self.process.pid, signal.SIGSTOP)
        try:

            def stopped() -> bool:
                pid, status = os.waitpid(self.process.pid, os.WUNTRACED | os.WNOHANG)
                return pid == self.process.pid and os.WIFSTOPPED(status)

            self.wait_until(stopped)
            yield
        finally:
            if self.process.poll() is None:
                os.kill(self.process.pid, signal.SIGCONT)

    def quit(self) -> None:
        self.send(b"q")
        assert self.process.wait(timeout=5) == 0
        assert termios.tcgetattr(self.master) == self.original_terminal

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        os.close(self.master)
