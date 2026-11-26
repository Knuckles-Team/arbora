"""Workspace file operations with path-escape refusal (ARBORA-OPS-R910).

The root is the setting ``workspace.root`` (environment ``ARBORA_WORKSPACE_ROOT``).
:func:`resolve_under_root` is the only path function: a path is resolved with
symlinks and must stay under the root, else ``PATH_ESCAPE``. A write is atomic
(a temporary file and a rename) and idempotent by path and digest; a stale
``expected_modified_at`` refuses ``WRITE_CONFLICT``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import os
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import OpSpec, Verb

READ_SCOPE = "workspace:read"
WRITE_SCOPE = "workspace:write"
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "workspace.files.write": "path-and-digest",
    "workspace.files.delete": "path",
}
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_LIST_ENTRIES = 1000
_Path = Field(min_length=1, max_length=4096)


class FilesListParams(Params):
    path: str = Field(default="", max_length=4096)


class FilesPathParams(Params):
    path: str = _Path


class FilesWriteParams(Params):
    path: str = _Path
    content_base64: str = Field(max_length=(MAX_FILE_BYTES // 3 + 1) * 4)
    expected_modified_at: str | None = Field(default=None, max_length=64)


def configured_root() -> Path:
    """The workspace root, or ``UNAVAILABLE`` when none is configured."""
    from arbora.core.settings import setting

    raw = str(setting("ARBORA_WORKSPACE_ROOT", "") or "").strip()
    if not raw:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the workspace root is not configured"}
        )
    return Path(raw).resolve()


def resolve_under_root(root: Path, path: str) -> Path:
    """``path`` under ``root`` with symlinks resolved, else ``PATH_ESCAPE``."""
    if "\x00" in path or Path(path).is_absolute():
        raise OperationRefused("PATH_ESCAPE")
    target = (root / path).resolve()
    if target != root and root not in target.parents:
        raise OperationRefused("PATH_ESCAPE")
    return target


def _modified(stat: os.stat_result) -> str:
    moment = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
    return moment.isoformat(timespec="microseconds").replace("+00:00", "Z")


async def handle_files_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    root = configured_root()
    target = resolve_under_root(root, params.get("path") or "")
    if not target.is_dir():
        raise OperationRefused("NOT_FOUND")
    entries: list[dict[str, Any]] = []
    for child in sorted(target.iterdir(), key=lambda item: item.name):
        if len(entries) >= MAX_LIST_ENTRIES:
            break
        try:
            resolve_under_root(root, str(child.relative_to(root)))
            stat = child.stat()
        except (OperationRefused, OSError):
            continue
        entries.append(
            {
                "name": child.name,
                "kind": "directory" if child.is_dir() else "file",
                "size": 0 if child.is_dir() else stat.st_size,
                "modified_at": _modified(stat),
            }
        )
    return {"value": {"entries": entries}}


async def handle_files_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    target = resolve_under_root(configured_root(), params["path"])
    if not target.is_file():
        raise OperationRefused("NOT_FOUND")
    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise OperationRefused("PAYLOAD_TOO_LARGE", {"limit": MAX_FILE_BYTES})
    media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return {
        "value": {
            "content_base64": base64.b64encode(target.read_bytes()).decode(),
            "size": size,
            "media_type": media,
        }
    }


async def handle_files_write(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    root = configured_root()
    target = resolve_under_root(root, params["path"])
    try:
        data = base64.b64decode(params["content_base64"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "content_base64"}) from exc
    if len(data) > MAX_FILE_BYTES:
        raise OperationRefused("PAYLOAD_TOO_LARGE", {"limit": MAX_FILE_BYTES})
    if target == root or target.is_dir():
        raise OperationRefused("INVALID_ARGUMENT", {"field": "path"})
    expected = params.get("expected_modified_at")
    if target.exists():
        if expected is not None and _modified(target.stat()) != expected:
            raise OperationRefused("WRITE_CONFLICT")
        if (
            hashlib.sha256(target.read_bytes()).digest()
            == hashlib.sha256(data).digest()
        ):
            return {"value": {"path": params["path"], "size": len(data)}}
    elif expected is not None:
        raise OperationRefused("WRITE_CONFLICT")
    target.parent.mkdir(parents=True, exist_ok=True)
    resolve_under_root(root, str(target.parent.relative_to(root)))
    handle, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"value": {"path": params["path"], "size": len(data)}}


async def handle_files_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    root = configured_root()
    target = resolve_under_root(root, params["path"])
    if target == root or not target.is_file():
        raise OperationRefused("NOT_FOUND")
    target.unlink()
    return {"value": {"deleted": True}}


def operations() -> tuple[OpSpec, ...]:
    h = "arbora.api.ops.workspace_files.handle_"
    return (
        build_read_op(
            verb=Verb.FIND,
            op_id="workspace.files.list",
            summary="List workspace files",
            examples=("list workspace files",),
            params=FilesListParams,
            result=EngineReply,
            handler=h + "files_list",
            scope=READ_SCOPE,
        ),
        build_read_op(
            verb=Verb.ASK,
            op_id="workspace.files.read",
            summary="Read one workspace file",
            examples=("read a workspace file",),
            params=FilesPathParams,
            result=EngineReply,
            handler=h + "files_read",
            scope=READ_SCOPE,
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="workspace.files.write",
            summary="Write one workspace file",
            examples=("write a workspace file",),
            params=FilesWriteParams,
            result=EngineReply,
            handler=h + "files_write",
            scope=WRITE_SCOPE,
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="workspace.files.delete",
            summary="Delete one workspace file",
            examples=("delete a workspace file",),
            params=FilesPathParams,
            result=EngineReply,
            handler=h + "files_delete",
            scope=WRITE_SCOPE,
        ),
    )


specs = operations
