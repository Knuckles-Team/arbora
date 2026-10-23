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
from .routes import _surface_for, caller_session_scope

#: The top-level directory listing is admitted before any frame is sent.
_TOP_LEVEL_OP = "source.tree"
#: Bound on the tree traversal and the streamed frame count of one fetch.
MAX_TREE_CALLS = 4096
MAX_ENTRIES = 100_000

DirectoryFileMap = dict[str, tuple[str | None, str | None]]


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
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope, status_code=status, headers=headers)


def _refusal_response(outcome: Any, *, digest: str, request_id: str) -> JSONResponse:
    """A denied top-level path: the standard error envelope, no content."""
    status, envelope = to_envelope(
        outcome,
        op=_TOP_LEVEL_OP,
        request_id=request_id or token_hex(12),
        registry_digest=digest,
    )
    headers = {"Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope, status_code=status, headers=headers)


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


def _malformed_path(path: str) -> bool:
    """Absolute, trailing-slash, traversal or doubled-slash path segments."""
    return (
        path.startswith("/")
        or path.endswith("/")
        or ".." in path.split("/")
        or "//" in path
    )


def _valid_cursor(after: str) -> bool:
    return after.startswith("dir:") or after.startswith("file:")


def _query_refusal(repo: str, ref: str, path: str, after: str | None) -> str | None:
    """The refusal code for one query set, or None when it is well-formed."""
    if not repo or not ref:
        return "INVALID_ARGUMENT"
    if path and _malformed_path(path):
        return "INVALID_ARGUMENT"
    if after is not None and not _valid_cursor(after):
        return "INVALID_ARGUMENT"
    return None


def _register_entry(
    entry: dict[str, Any],
    directories: list[str],
    files: DirectoryFileMap,
) -> str | None:
    """Record one listing entry; return the directory to descend into next."""
    kind = entry.get("kind")
    entry_path = entry.get("path")
    if not isinstance(entry_path, str) or not entry_path:
        return None
    if kind == "directory":
        # Resolution is recursive: every directory is itself admitted
        # through the chokepoint before it streams.
        directories.append(entry_path)
        return entry_path
    if kind != "file":
        return None
    digest_value = entry.get("content_digest")
    revision = entry.get("revision_id")
    files[entry_path] = (
        digest_value if isinstance(digest_value, str) else None,
        revision if isinstance(revision, str) else None,
    )
    return None


async def _collect_entries(
    *,
    repo: str,
    ref: str,
    path: str,
    caller: Any,
    surface: Any,
    services: Any,
    invoke: Any,
) -> tuple[list[str], DirectoryFileMap, tuple[str, str] | None]:
    """Walk the tree breadth-first; (directories, files) or a refusal pair."""
    directories: list[str] = []
    files: DirectoryFileMap = {}
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
            descended = _register_entry(entry, directories, files)
            if descended is not None:
                worklist.append(descended)
    return directories, files, error


def _frame_keys(directories: list[str], files: DirectoryFileMap) -> list[str]:
    """Directories before files; both groups in stable path order."""
    directory_keys = sorted("dir:" + name for name in directories)
    file_keys = sorted("file:" + name for name in files)
    return directory_keys + file_keys


def _file_frame(key: str, files: DirectoryFileMap) -> bytes:
    name = key[len("file:") :]
    file_digest, revision = files[name]
    frame: dict[str, Any] = {"kind": "file", "path": name, "cursor": key}
    if file_digest is not None:
        frame["digest"] = file_digest
    if revision is not None:
        frame["revision"] = revision
    return _frame(frame)


def _end_frame(limited: bool) -> bytes:
    payload: dict[str, Any] = {"kind": "end", "complete": not limited}
    if limited:
        payload["reason"] = "entry-limit"
    return _frame(payload)


def _resume_error_frame() -> bytes:
    return _frame(
        {
            "kind": "error",
            "code": "INVALID_ARGUMENT",
            "reason": "unknown resume cursor",
        }
    )


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
        params = request.query_params
        repo = params.get("repo", "").strip()
        ref = params.get("ref", "").strip()
        path = params.get("path", "").strip()
        after = params.get("after")
        code = _query_refusal(repo, ref, path, after)
        if code is not None:
            return _error(code, digest=digest, request_id=caller.request_id)
        surface = _surface_for(request, caller, is_console_request)

        # The requested subtree root is admitted before any content is sent.
        with caller_session_scope(caller):
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
            with caller_session_scope(caller):
                directories, files, error = await _collect_entries(
                    repo=repo,
                    ref=ref,
                    path=path,
                    caller=caller,
                    surface=surface,
                    services=services,
                    invoke=invoke,
                )
            if error is not None:
                yield _frame({"kind": "error", "code": error[0], "reason": error[1]})
                return
            keys = _frame_keys(directories, files)
            start = 0
            if after is not None:
                if after not in keys:
                    yield _resume_error_frame()
                    return
                start = keys.index(after) + 1
            limited = len(keys) > MAX_ENTRIES
            for index, key in enumerate(keys[start : start + MAX_ENTRIES]):
                if key.startswith("dir:"):
                    yield _frame(
                        {"kind": "dir", "path": key[len("dir:") :], "cursor": key}
                    )
                else:
                    yield _file_frame(key, files)
                if limited and index + start == MAX_ENTRIES:
                    break
            yield _end_frame(limited)

        return StreamingResponse(
            stream(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )

    endpoint.__name__ = "http_source_checkout"
    return endpoint


__all__ = ["MAX_ENTRIES", "MAX_TREE_CALLS", "make_checkout_endpoint"]
