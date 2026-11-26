"""Workspace file operations with path-escape refusal (ARBORA-OPS-R910)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import workspace_files as wf
from tests.api._knowledge_support import NO_OP

pytestmark = pytest.mark.spec("ARBORA-OPS-R910")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setenv("ARBORA_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setattr(wf, "configured_root", lambda: workspace.resolve())
    return workspace


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def _code(awaitable: Any) -> str:
    with pytest.raises(OperationRefused) as refused:
        await awaitable
    return refused.value.code


async def test_dotdot_and_symlink_escape_are_refused(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (root / "link").symlink_to(outside)
    (root / "dirlink").symlink_to(tmp_path)
    for path in ("../outside.txt", "link", "dirlink/outside.txt", "/etc/passwd"):
        assert (
            await _code(wf.handle_files_read(None, {"path": path}, NO_OP))
            == "PATH_ESCAPE"
        )
        write = wf.handle_files_write(
            None, {"path": path, "content_base64": _b64(b"x")}, NO_OP
        )
        assert await _code(write) == "PATH_ESCAPE"
    assert outside.read_text() == "secret"
    listed = await wf.handle_files_list(None, {}, NO_OP)
    assert [e["name"] for e in listed["value"]["entries"]] == []


async def test_write_read_list_delete_round_trip(root: Path) -> None:
    written = await wf.handle_files_write(
        None, {"path": "a/b.txt", "content_base64": _b64(b"hello")}, NO_OP
    )
    assert written["value"] == {"path": "a/b.txt", "size": 5}
    read = await wf.handle_files_read(None, {"path": "a/b.txt"}, NO_OP)
    assert base64.b64decode(read["value"]["content_base64"]) == b"hello"
    assert read["value"]["media_type"] == "text/plain"
    listed = await wf.handle_files_list(None, {"path": "a"}, NO_OP)
    assert [(e["name"], e["kind"], e["size"]) for e in listed["value"]["entries"]] == [
        ("b.txt", "file", 5)
    ]
    assert (await wf.handle_files_delete(None, {"path": "a/b.txt"}, NO_OP))[
        "value"
    ] == {"deleted": True}
    assert (
        await _code(wf.handle_files_read(None, {"path": "a/b.txt"}, NO_OP))
        == "NOT_FOUND"
    )


async def test_write_conflict_on_stale_modified_at(root: Path) -> None:
    await wf.handle_files_write(
        None, {"path": "f", "content_base64": _b64(b"1")}, NO_OP
    )
    stamp = wf._modified((root / "f").stat())
    ok = await wf.handle_files_write(
        None,
        {"path": "f", "content_base64": _b64(b"2"), "expected_modified_at": stamp},
        NO_OP,
    )
    assert ok["value"]["size"] == 1
    stale = wf.handle_files_write(
        None,
        {
            "path": "f",
            "content_base64": _b64(b"3"),
            "expected_modified_at": "2000-01-01T00:00:00.000000Z",
        },
        NO_OP,
    )
    assert await _code(stale) == "WRITE_CONFLICT"
    assert (root / "f").read_bytes() == b"2"


async def test_size_limit_is_refused(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wf, "MAX_FILE_BYTES", 4)
    big = wf.handle_files_write(
        None, {"path": "f", "content_base64": _b64(b"12345")}, NO_OP
    )
    assert await _code(big) == "PAYLOAD_TOO_LARGE"
    (root / "g").write_bytes(b"12345")
    assert (
        await _code(wf.handle_files_read(None, {"path": "g"}, NO_OP))
        == "PAYLOAD_TOO_LARGE"
    )
    assert not (root / "f").exists()


async def test_an_unconfigured_root_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARBORA_WORKSPACE_ROOT", raising=False)
    assert await _code(wf.handle_files_list(None, {}, NO_OP)) == "UNAVAILABLE"
