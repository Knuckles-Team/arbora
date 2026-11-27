"""The git smart-HTTP read bridge (ARBORA-OPS-R851).

``GET /git/{repository}/info/refs?service=git-upload-pack`` advertises the repository's refs
and ``POST /git/{repository}/git-upload-pack`` streams the engine's pack of the wanted
revisions. The routes carry no authority logic of their own: each request authenticates
through the shared HTTP authenticator and drives ``source.git.refs``, ``source.git.pack`` and
``source.git.pack_chunk`` through the single invocation chokepoint, so admission is the
same as ``source.tree``. A path outside the caller's read grant refuses the whole request
with HTTP 403 before any pack byte is sent. The bridge serves no push: ``git-receive-pack``
answers HTTP 403 ``push_not_served``. Arbora holds no pack: each chunk passes from the
engine blob store to the socket.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from secrets import token_hex
from types import MappingProxyType
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from arbora.api.errors import ArboraErrorCode, ArboraRefusal, to_envelope

from .auth import HTTPAuthenticationError
from .routes import MAX_BODY_BYTES, _surface_for, caller_session_scope

REFS_OP = "source.git.refs"
PACK_OP = "source.git.pack"
CHUNK_OP = "source.git.pack_chunk"
UPLOAD = "git-upload-pack"
RECEIVE = "git-receive-pack"
_HEADS = "refs/heads/"
_ZERO = "0" * 40
AGENT = "arbora-git-bridge"
NO_STORE = MappingProxyType({"Cache-Control": "no-store"})


def pkt(data: bytes) -> bytes:
    """One pkt-line: four hex digits of the length including themselves, then the data."""
    return b"%04x" % (len(data) + 4) + data


FLUSH = b"0000"


def parse_request(body: bytes) -> tuple[list[str], list[str], bool]:
    """The ``want`` and ``have`` object ids of one upload-pack body, and whether it is done."""
    wants: list[str] = []
    haves: list[str] = []
    done = False
    cursor = 0
    while cursor < len(body):
        try:
            size = int(body[cursor : cursor + 4], 16)
        except ValueError:
            raise ValueError("malformed pkt-line length") from None
        if size == 0:
            cursor += 4
            continue
        if size < 4 or cursor + size > len(body):
            raise ValueError("truncated pkt-line")
        line = body[cursor + 4 : cursor + size].decode("ascii", "replace").strip()
        cursor += size
        word, _, rest = line.partition(" ")
        if word == "want":
            wants.append(rest.split(" ", 1)[0])
        elif word == "have":
            haves.append(rest.split(" ", 1)[0])
        elif word == "done":
            done = True
    return wants, haves, done


def _primary(refs: list[dict[str, str]]) -> dict[str, str] | None:
    """The branch HEAD points at: ``main`` when listed, else the first head."""
    heads = [r for r in refs if r["ref"].startswith(_HEADS)]
    return next((r for r in heads if r["ref"] == _HEADS + "main"), None) or (
        heads[0] if heads else None
    )


def _capabilities(refs: list[dict[str, str]], primary: dict[str, str] | None) -> str:
    fmt = "sha256" if any(len(r["object_id"]) == 64 for r in refs) else "sha1"
    caps = f"agent={AGENT} object-format={fmt}"
    return caps + (f" symref=HEAD:{primary['ref']}" if primary else "")


def _ref_lines(rows: list[dict[str, str]], caps: str) -> list[bytes]:
    """One pkt-line per ref; the first carries the capabilities, an empty list a null ref."""
    if not rows:
        return [pkt(f"{_ZERO} capabilities^{{}}\0{caps}\n".encode())]
    tails = [f"\0{caps}"] + [""] * (len(rows) - 1)
    return [
        pkt(f"{row['object_id']} {row['ref']}{tail}\n".encode())
        for row, tail in zip(rows, tails, strict=True)
    ]


def advertisement(refs: list[dict[str, str]]) -> bytes:
    """The smart-HTTP ref advertisement of ``git-upload-pack``."""
    primary = _primary(refs)
    head = [{"ref": "HEAD", "object_id": primary["object_id"]}] if primary else []
    rows = head + sorted(refs, key=lambda r: r["ref"])
    caps = _capabilities(refs, primary)
    return b"".join(
        [pkt(b"# service=git-upload-pack\n"), FLUSH, *_ref_lines(rows, caps), FLUSH]
    )


def branch_of(refs: list[dict[str, str]], wants: list[str]) -> str | None:
    """The one branch whose head every want names, or ``None``."""
    common: set[str] | None = None
    for want in wants:
        names = {
            r["ref"].removeprefix(_HEADS)
            for r in refs
            if r["object_id"] == want and r["ref"].startswith(_HEADS)
        }
        common = names if common is None else common & names
    return sorted(common)[0] if common else None


def _admitted(outcome: Any) -> bool:
    return not hasattr(outcome, "source") and getattr(outcome, "code", "OK") == "OK"


def _value(outcome: Any) -> dict[str, Any]:
    value = getattr(outcome, "value", outcome)
    inner = value.get("value") if isinstance(value, dict) else None
    return (
        inner if isinstance(inner, dict) else (value if isinstance(value, dict) else {})
    )


def _push_refused() -> Response:
    return JSONResponse(
        {"error": {"code": "POLICY_DENIED", "reason": "push_not_served"}},
        status_code=403,
        headers={"Cache-Control": "no-store"},
    )


def mount_git_bridge(
    app: FastAPI,
    *,
    services: Any,
    authenticate: Any,
    invoke: Any,
    is_console_request: Any = None,
) -> None:
    """Add the git smart-HTTP routes to ``app``; ``invoke`` is the one chokepoint."""
    digest = services.registry.digest

    def refuse(outcome: Any, op: str, request_id: str = "") -> Response:
        status, envelope = to_envelope(
            outcome,
            op=op,
            request_id=request_id or token_hex(12),
            registry_digest=digest,
        )
        headers = {"Cache-Control": "no-store"}
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(envelope, status_code=status, headers=headers)

    def code(name: str, op: str, request_id: str = "") -> Response:
        return refuse(ArboraRefusal(ArboraErrorCode(name)), op, request_id)

    async def step(
        request: Request, caller: Any, op: str, params: dict[str, Any]
    ) -> Any:
        surface = _surface_for(request, caller, is_console_request)
        with caller_session_scope(caller):
            return await invoke(op, params, caller, surface, services=services)

    async def info_refs(request: Request, repository: str) -> Response:
        service = request.query_params.get("service", "")
        if service == RECEIVE:
            return _push_refused()
        if service != UPLOAD:
            return code("INVALID_ARGUMENT", REFS_OP)
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return code("UNAUTHENTICATED", REFS_OP)
        outcome = await step(request, caller, REFS_OP, {"repository": repository})
        if not _admitted(outcome):
            return refuse(outcome, REFS_OP, caller.request_id)
        return Response(
            advertisement(list(_value(outcome).get("refs", []))),
            media_type=f"application/x-{UPLOAD}-advertisement",
            headers={"Cache-Control": "no-store"},
        )

    async def negotiate(
        request: Request, caller: Any, repository: str
    ) -> Response | tuple[dict[str, Any], bool]:
        """The export params of one fetch and whether the client is done negotiating."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return code("INVALID_ARGUMENT", PACK_OP, caller.request_id)
        try:
            wants, haves, done = parse_request(await request.body())
        except ValueError:
            return code("INVALID_ARGUMENT", PACK_OP, caller.request_id)
        listed = await step(request, caller, REFS_OP, {"repository": repository})
        if not _admitted(listed):
            return refuse(listed, REFS_OP, caller.request_id)
        branch = branch_of(list(_value(listed).get("refs", [])), wants)
        if not wants or branch is None:
            return code("INVALID_ARGUMENT", PACK_OP, caller.request_id)
        params = {"repository": repository, "branch": branch}
        return {**params, "wants": wants, "haves": haves}, done

    async def upload_pack(request: Request, repository: str) -> Response:
        try:
            caller = await authenticate(request)
        except HTTPAuthenticationError:
            return code("UNAUTHENTICATED", PACK_OP)
        negotiated = await negotiate(request, caller, repository)
        if isinstance(negotiated, Response):
            return negotiated
        params, done = negotiated
        media = f"application/x-{UPLOAD}-result"
        if not done:
            return Response(pkt(b"NAK\n"), media_type=media, headers=NO_STORE)
        exported = await step(request, caller, PACK_OP, params)
        if not _admitted(exported):
            return refuse(exported, PACK_OP, caller.request_id)
        pack_ref = str(_value(exported)["pack_ref"])
        first = await step(
            request, caller, CHUNK_OP, {"pack_ref": pack_ref, "index": 0}
        )
        if not _admitted(first):
            return refuse(first, CHUNK_OP, caller.request_id)

        async def body() -> AsyncIterator[bytes]:
            yield pkt(b"NAK\n")
            outcome = first
            for index in range(int(_value(first).get("chunks", 0))):
                if index:
                    outcome = await step(
                        request,
                        caller,
                        CHUNK_OP,
                        {"pack_ref": pack_ref, "index": index},
                    )
                    if not _admitted(outcome):
                        raise RuntimeError("pack chunk refused after the stream began")
                yield base64.b64decode(_value(outcome).get("data_b64", ""))

        return StreamingResponse(body(), media_type=media, headers=NO_STORE)

    async def receive_pack(request: Request, repository: str) -> Response:
        return _push_refused()

    app.add_api_route(
        "/git/{repository:path}/info/refs",
        info_refs,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        f"/git/{{repository:path}}/{UPLOAD}",
        upload_pack,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        f"/git/{{repository:path}}/{RECEIVE}",
        receive_pack,
        methods=["POST"],
        include_in_schema=False,
    )


__all__ = ["advertisement", "branch_of", "mount_git_bridge", "parse_request", "pkt"]
