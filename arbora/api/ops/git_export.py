"""Served git export operations for the smart-HTTP bridge (ARBORA-OPS-R851).

``source.git.refs`` reads the repository's refs and ``source.git.pack`` asks the engine to
export a pack of wants and haves (``GitRefsList`` and ``GitExportPack``, MEDULLA-SCM-R014).
``source.git.pack_chunk`` reads one chunk of the exported pack from the engine blob store.
Arbora holds no pack bytes and no ref: it admits as ``source.tree`` does (the same read
scopes, the engine's own path ACL) and streams what the engine returns. Both methods are
engine ``main`` shapes the pinned release lacks, so they refuse ``UNAVAILABLE`` naming the
method until the re-pin.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params
from arbora.api.ops.engine_calls import unavailable
from arbora.api.ops.pin_guards import send_main
from arbora.api.ops.source import _read_op
from arbora.api.registry import OpSpec

_NAME = Field(min_length=1, max_length=1024)
_OID = Field(min_length=40, max_length=64, pattern=r"^[0-9a-f]+$")
#: The most wants or haves one fetch carries.
MAX_OBJECT_IDS = 256
#: The content-addressed form of a pack reference (the receipt's ``pack_ref``).
CAS_PREFIX = "cas:sha256:"
READ_SCOPES = frozenset({"node:read", "edge:read", "blob:read"})


class GitRefsParams(Params):
    repository: str = _NAME


class GitRefsResult(Params):
    refs: list[dict[str, str]]


class GitPackParams(Params):
    repository: str = _NAME
    branch: str = _NAME
    wants: list[str] = Field(min_length=1, max_length=MAX_OBJECT_IDS)
    haves: list[str] = Field(default_factory=list, max_length=MAX_OBJECT_IDS)


class GitPackResult(Params):
    object_format: str
    pack_ref: str
    pack_bytes: int = Field(ge=0)


class GitPackChunkParams(Params):
    pack_ref: str = Field(min_length=len(CAS_PREFIX) + 1, max_length=128)
    index: int = Field(ge=0)


class GitPackChunkResult(Params):
    chunks: int
    data_b64: str


async def handle_git_refs(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The refs of one repository, as the engine lists them."""
    answer = await send_main(
        context,
        "GitRefsList",
        {"request": {"repo": params["repository"]}},
        models=("GitRefsListRequest",),
    )
    rows = answer.get("refs") if isinstance(answer, Mapping) else answer
    if not isinstance(rows, list):
        raise unavailable("engine result malformed")
    try:
        return {
            "refs": [
                {"ref": str(r["ref"]), "object_id": str(r["object_id"])} for r in rows
            ]
        }
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


async def handle_git_pack(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Export the wants beyond the haves; the whole request is refused on one denied path."""
    from arbora.core.settings import runtime_config

    receipt = await send_main(
        context,
        "GitExportPack",
        {
            "request": {
                "repo": params["repository"],
                "branch": params["branch"],
                "wants": list(params["wants"]),
                "haves": list(params["haves"]),
            }
        },
        models=("GitExportPackRequest", "GitExportPackReceipt"),
    )
    try:
        result = {
            "object_format": str(receipt["object_format"]),
            "pack_ref": str(receipt["pack_ref"]),
            "pack_bytes": int(receipt["pack_bytes"]),
        }
    except (KeyError, TypeError, ValueError):
        raise unavailable("engine result malformed") from None
    if not result["pack_ref"].startswith(CAS_PREFIX):
        raise unavailable("engine result malformed")
    if result["pack_bytes"] > int(runtime_config.git_bridge_max_pack_bytes):
        raise OperationRefused("LOAD_CAP_EXCEEDED", {"reason": "git_pack_too_large"})
    return result


async def handle_git_pack_chunk(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One chunk of an exported pack; a foreign reference answers like an absent one."""
    from arbora.api.ops.bundle import handle_bundle_download

    ref = str(params["pack_ref"])
    if not ref.startswith(CAS_PREFIX):
        raise OperationRefused("NOT_FOUND", {})
    chunk = await handle_bundle_download(
        context,
        {"bundle_ref": ref.removeprefix(CAS_PREFIX), "index": params["index"]},
        op,
    )
    base64.b64decode(chunk["data_b64"], validate=True)
    return chunk


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.git_export"
    return (
        _read_op(
            op_id="source.git.refs",
            summary="List the refs of one repository for the git smart-HTTP bridge",
            examples=("list the git refs of this repository",),
            params=GitRefsParams,
            result=GitRefsResult,
            handler=f"{module}.handle_git_refs",
            scopes=frozenset({"node:read", "edge:read"}),
        ),
        _read_op(
            op_id="source.git.pack",
            summary="Export a git pack of wanted revisions beyond the haves",
            examples=("export a pack for these wanted commits",),
            params=GitPackParams,
            result=GitPackResult,
            handler=f"{module}.handle_git_pack",
            scopes=READ_SCOPES,
        ),
        _read_op(
            op_id="source.git.pack_chunk",
            summary="Read one chunk of an exported git pack",
            examples=("read the next chunk of the exported pack",),
            params=GitPackChunkParams,
            result=GitPackChunkResult,
            handler=f"{module}.handle_git_pack_chunk",
            scopes=READ_SCOPES,
        ),
    )


specs = operations

__all__ = [
    "CAS_PREFIX",
    "MAX_OBJECT_IDS",
    "handle_git_pack",
    "handle_git_pack_chunk",
    "handle_git_refs",
    "operations",
    "specs",
]
