"""The virtual checkout streaming endpoint (ARBORA-OPS-R850).

Streams one repository ref as NDJSON entry frames: every directory first,
then every file with its content digest. The endpoint carries no authority
logic of its own; it drives the admitted ``source.tree`` operation through
the single invocation chokepoint, so each directory listing is admitted per
principal and per path exactly as a direct ``source.tree`` call would be.
A denied path refuses before content is sent; a resumed fetch continues
from the last acknowledged entry (its frame ``cursor``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from secrets import token_hex
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from arbora.api.errors import to_envelope

from .auth import HTTPAuthenticationError
from .routes import _surface_for

#: The top-level directory listing is admitted before any frame is sent.
_TOP_LEVEL_OP = "source.tree"
#: Bound on the tree traversal and the streamed frame count of one fetch.
MAX_TREE_CALLS = 4096
MAX_ENTRIES = 100_000


def _frame(payload: dict[str, Any]) -> bytes:
    """One NDJSON frame; keys are sorted so frames are byte-stable."""
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode()


def _error(code: str, *, digest: str, request_id: str) -> JSONResponse:
    from arbora.api.errors import ArboraErrorCode, ArboraRefusal

    status, envelope = to_envelope(
        ArboraRefusal(ArboraErrorCode(code)),
        op=_TOP_LEVEL_OP,
        request_id=request_id or token_hex(12),
        registry_digest=digest,
    )
    return JSONResponse(
        envelope, status_code=status, headers={"Cache-Control": "no-store"}
    )


def _refusal_response(outcome: Any, *, digest: str, request_id: str) -> JSONResponse:
    """A denied top-level path: the standard error envelope, no content."""
    status, envelope = to_envelope(
        outcome,
        op=_TOP_LEVEL_OP,
        request_id=request_id or token_hex(12),
        registry_digest=digest,
    )
    return JSONResponse(
        envelope, status_code=status, headers={"Cache-Control": "no-store"}
    )


def _admitted(outcome: Any) -> bool:
    return not hasattr(outcome, "source") and getattr(outcome, "code", "OK") == "OK"


def _entries(outcome: Any) -> list[dict[str, Any]]:
    value = outcome.value
    if not isinstance(value, dict):
        raise ValueError("checkout listing has no value")
    inner = value.get("value")
    if not isinstance(inner, dict):
        raise ValueError("checkout listing has no entries")
    entries = inner.get("entries")
    if not isinstance(entries, list):
        raise ValueError("checkout listing has no entries")
    return [entry for entry in entries if isinstance(entry, dict)]


def make_checkout_endpoint(
    *,
    services: Any,
    authenticate: Any,
    invoke: Any,
    is_console_request: Any = None,
) -> Any:
    """Return the GET handler of the virtual checkout stream.

    ``invoke`` is the single invocation chokepoint (ARBORA-OPS-R007); the
    endpoint never authenticates or admits a path itself.
    """

    async def endpoint(request: Request) -> Any:
        digest = services.registry.digest
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return _error("UNAUTHENTICATED", digest=digest, request_id="")
        repo = request.query_params.get("repo", "").strip()
        ref = request.query_params.get("ref", "").strip()
        path = request.query_params.get("path", "").strip()
        after = request.query_params.get("after")
        if not repo or not ref:
            return _error(
                "INVALID_ARGUMENT", digest=digest, request_id=caller.request_id
            )
        if path and (
            path.startswith("/")
            or path.endswith("/")
            or ".." in path.split("/")
            or "//" in path
        ):
            return _error(
                "INVALID_ARGUMENT", digest=digest, request_id=caller.request_id
            )
        if after is not None and not (
            after.startswith("dir:") or after.startswith("file:")
        ):
            return _error(
                "INVALID_ARGUMENT", digest=digest, request_id=caller.request_id
            )
        surface = _surface_for(request, caller, is_console_request)

        # The requested subtree root is admitted before any content is sent.
        outcome = await invoke(
            _TOP_LEVEL_OP,
            {"repository": repo, "ref": ref, "path": path},
            caller,
            surface,
            services=services,
        )
        if not _admitted(outcome):
            return _refusal_response(
                outcome, digest=digest, request_id=caller.request_id
            )

        async def stream() -> AsyncIterator[bytes]:
            yield _frame({"kind": "start", "repo": repo, "ref": ref, "path": path})
            directories: list[str] = []
            files: dict[str, tuple[str | None, str | None]] = {}
            worklist: list[str] = [path]
            calls = 0
            error: tuple[str, str] | None = None
            while worklist and error is None:
                directory = worklist.pop(0)
                calls += 1
                if calls > MAX_TREE_CALLS:
                    error = ("UNAVAILABLE", "tree traversal limit exceeded")
                    break
                listing = await invoke(
                    _TOP_LEVEL_OP,
                    {"repository": repo, "ref": ref, "path": directory},
                    caller,
                    surface,
                    services=services,
                )
                if not _admitted(listing):
                    error = (
                        str(listing.code),
                        "a subpath of the checkout is not admitted",
                    )
                    break
                for entry in _entries(listing):
                    kind = entry.get("kind")
                    entry_path = entry.get("path")
                    if not isinstance(entry_path, str) or not entry_path:
                        continue
                    if kind == "directory":
                        # Resolution is recursive: every directory is itself
                        # admitted through the chokepoint before it streams.
                        directories.append(entry_path)
                        worklist.append(entry_path)
                    elif kind == "file":
                        digest_value = entry.get("content_digest")
                        revision = entry.get("revision_id")
                        files[entry_path] = (
                            digest_value if isinstance(digest_value, str) else None,
                            revision if isinstance(revision, str) else None,
                        )
            if error is not None:
                yield _frame({"kind": "error", "code": error[0], "reason": error[1]})
                return
            directory_keys = sorted("dir:" + name for name in directories)
            file_keys = sorted("file:" + name for name in files)
            keys = directory_keys + file_keys
            start = 0
            if after is not None:
                if after not in keys:
                    yield _frame(
                        {
                            "kind": "error",
                            "code": "INVALID_ARGUMENT",
                            "reason": "unknown resume cursor",
                        }
                    )
                    return
                start = keys.index(after) + 1
            limited = len(keys) > MAX_ENTRIES
            for index, key in enumerate(keys[start : start + MAX_ENTRIES]):
                if key.startswith("dir:"):
                    yield _frame(
                        {"kind": "dir", "path": key[len("dir:") :], "cursor": key}
                    )
                else:
                    file_digest, revision = files[key[len("file:") :]]
                    frame: dict[str, Any] = {
                        "kind": "file",
                        "path": key[len("file:") :],
                        "cursor": key,
                    }
                    if file_digest is not None:
                        frame["digest"] = file_digest
                    if revision is not None:
                        frame["revision"] = revision
                    yield _frame(frame)
                if limited and index + start == MAX_ENTRIES:
                    break
            yield _frame(
                {
                    "kind": "end",
                    "complete": not limited,
                    **({"reason": "entry-limit"} if limited else {}),
                }
            )

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )

    endpoint.__name__ = "http_source_checkout"
    return endpoint


__all__ = ["MAX_ENTRIES", "MAX_TREE_CALLS", "make_checkout_endpoint"]
