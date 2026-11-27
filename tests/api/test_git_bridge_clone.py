"""A real ``git clone`` against the served git bridge on a loopback server (ARBORA-OPS-R851).

The bridge is the real served composition; the engine behind it is the recording double of
``tests/api/test_git_bridge_served.py``, holding a pack that this test builds with real git
from a real repository. The app is served by uvicorn on 127.0.0.1 in a thread, and the stock
``git`` binary clones through it with a bearer header. Owner run: this needs ``git`` and a
free loopback port.
"""

from __future__ import annotations

import hashlib
import shutil
import socket
import subprocess  # nosec B404 - the test drives the stock git binary
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.api import test_git_bridge_served as bridge

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

TENANT = "tenant:one"


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(  # nosec B603 B607 - fixed argv of the stock git binary
        ["git", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )
    return done.stdout


def _source(root: Path) -> tuple[str, bytes]:
    """A one-commit repository: its HEAD id and the pack of everything HEAD reaches."""
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("bridge clone\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    pack = _git(root, "pack-objects", "--revs", "--stdout", stdin=b"HEAD\n")
    return head, pack


class _Loopback:
    """The served ASGI app on a free 127.0.0.1 port, in a thread."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Loopback:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)  # test-only startup poll of a real socket, not a clock
        assert self.server.started, "the loopback server did not start"
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def served_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[_Loopback, str]]:
    head, pack = _source(tmp_path / "source")
    digest = hashlib.sha256(pack).hexdigest()

    class RealPack(bridge.GitEngine):
        def _m_GitRefsList(self, p: dict[str, Any]) -> Any:
            return {"refs": [{"ref": "refs/heads/main", "object_id": head}]}

        def _m_GitExportPack(self, p: dict[str, Any]) -> Any:
            self.store.blobs[(self.tenant, digest)] = pack
            return {
                "object_format": "sha1",
                "pack_ref": f"cas:sha256:{digest}",
                "pack_bytes": len(pack),
            }

        def _m_BlobChunkGet(self, p: dict[str, Any]) -> Any:
            blob = self.store.blobs[(self.tenant, digest)]
            return blob[p["idx"] * 1_048_576 : (p["idx"] + 1) * 1_048_576]

    monkeypatch.setattr(bridge.base, "_Engine", RealPack)
    bridge._declare(monkeypatch)
    app, _engine = bridge.base._app(
        monkeypatch, bridge.base._Store(), bridge.SOURCE_READ
    )
    with _Loopback(app) as loop:
        yield loop, head


@pytest.mark.spec("ARBORA-OPS-R851")
def test_a_real_git_clone_fetches_the_served_history(
    served_repo: tuple[_Loopback, str], tmp_path: Path
) -> None:
    loop, head = served_repo
    url = f"http://127.0.0.1:{loop.port}/api/v1/git/repo"
    auth = f"Authorization: Bearer fixture-verified-{TENANT}"
    target = tmp_path / "clone"
    _git(
        tmp_path,
        "-c",
        f"http.extraHeader={auth}",
        "clone",
        "-q",
        "--branch",
        "main",
        url,
        str(target),
    )
    assert _git(target, "rev-parse", "HEAD").decode().strip() == head
    assert (target / "README.md").read_text() == "bridge clone\n"
    # Push is never served: a real push is refused with the bridge's reason.
    (target / "more.txt").write_text("x\n")
    _git(target, "add", "more.txt")
    _git(target, "commit", "-q", "-m", "second")
    with pytest.raises(subprocess.CalledProcessError) as pushed:
        _git(target, "-c", f"http.extraHeader={auth}", "push", "origin", "main")
    assert b"403" in pushed.value.stderr or b"push_not_served" in pushed.value.stderr
