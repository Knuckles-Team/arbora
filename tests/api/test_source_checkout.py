"""Virtual checkout stream: directories first, resumable, fail-closed (R850).

The endpoint carries no authority logic of its own: it drives the admitted
``source.tree`` operation through the single invocation chokepoint, so the
tests fake ``invoke`` (the pipeline contract is covered by the invoke
suite) and prove the stream shape, the resume cursor and the denials.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from arbora.api.http.app import create_api_application
from arbora.api.http.auth import HTTPAuthenticationError
from arbora.api.invoke import OpError, OpResult
from arbora.api.ops import source as source_ops
from arbora.api.registry import Registry
from tests.api._support import make_verified_caller

pytestmark = pytest.mark.spec("ARBORA-OPS-R850")

caller = make_verified_caller("node:read")
FULL_SCOPES = frozenset({"node:read", "edge:read", "blob:read"})

REPOSITORY = "repo-a"
REF = "main"

#: The one admitted ref's projection, as the tree operation would list it.
TREE: dict[str, list[dict[str, Any]]] = {
    "": [
        {
            "path": "README.md",
            "kind": "file",
            "content_digest": "digest:readme",
            "revision_id": "rev:101",
        },
        {
            "path": "docs",
            "kind": "directory",
            "content_digest": None,
            "revision_id": None,
        },
        {
            "path": "src",
            "kind": "directory",
            "content_digest": None,
            "revision_id": None,
        },
    ],
    "docs": [
        {
            "path": "docs/guide.md",
            "kind": "file",
            "content_digest": "digest:guide",
            "revision_id": "rev:104",
        },
    ],
    "src": [
        {
            "path": "src/app.py",
            "kind": "file",
            "content_digest": "digest:app",
            "revision_id": "rev:102",
        },
        {
            "path": "src/deep",
            "kind": "directory",
            "content_digest": None,
            "revision_id": None,
        },
    ],
    "src/deep": [
        {
            "path": "src/deep/nested.txt",
            "kind": "file",
            "content_digest": "digest:deep",
            "revision_id": "rev:103",
        },
    ],
}

EXPECTED_DIRS = ["docs", "src", "src/deep"]
EXPECTED_FILES = [
    ("README.md", "digest:readme", "rev:101"),
    ("docs/guide.md", "digest:guide", "rev:104"),
    ("src/app.py", "digest:app", "rev:102"),
    ("src/deep/nested.txt", "digest:deep", "rev:103"),
]


def _listing(path: str) -> OpResult:
    entries = tuple(source_ops.SourceTreeEntry(**entry) for entry in TREE[path])
    outcome = source_ops.SourceTreeOutcome(
        tenant="tenant:one",
        repository=REPOSITORY,
        ref=REF,
        path=path,
        found=True,
        entries=entries,
    )
    return OpResult(value={"value": outcome.model_dump(mode="json")})


class FakeAuthenticator:
    def __init__(self, denied: bool = False) -> None:
        self.denied = denied
        self.calls = 0

    async def authenticate(self, request) -> Any:
        self.calls += 1
        if self.denied:
            raise HTTPAuthenticationError("verified identity required")
        return caller(effective_scopes=FULL_SCOPES)

    def is_console_request(self, request, caller) -> bool:
        return False


def _app(invoke: Any, authenticator: FakeAuthenticator | None = None) -> FastAPI:
    sub = create_api_application(
        services=SimpleNamespace(registry=Registry(source_ops.operations())),
        visibility=lambda op, _caller: True,
        authenticator=authenticator or FakeAuthenticator(),
        invoke=invoke,
    )
    root = FastAPI()
    root.mount("/api/v1", sub)
    return root


def _frames(body: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in body.splitlines() if line]


async def _fetch(app: FastAPI, query: dict[str, str]) -> tuple[int, str, str]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://served"
    ) as http:
        async with http.stream(
            "GET", "/api/v1/source/checkout", params=query
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])
            return response.status_code, response.headers.get("content-type", ""), body


@pytest.mark.spec("ARBORA-OPS-R850")
async def test_checkout_stream_resumable():
    calls: list[tuple[str, dict[str, Any], Any, Any]] = []

    async def fake_invoke(op_id, params, caller, surface, *, services):
        assert op_id == "source.tree"
        calls.append((op_id, dict(params), caller, surface))
        return _listing(params["path"])

    app = _app(fake_invoke)
    status, content_type, body = await _fetch(app, {"repo": REPOSITORY, "ref": REF})
    assert status == 200
    assert content_type.startswith("application/x-ndjson")
    frames = _frames(body)

    assert frames[0] == {"kind": "start", "repo": REPOSITORY, "ref": REF, "path": ""}
    assert frames[-1] == {"kind": "end", "complete": True}
    middle = frames[1:-1]
    kinds = [frame["kind"] for frame in middle]
    # Every directory streams before any file.
    dir_positions = [i for i, kind in enumerate(kinds) if kind == "dir"]
    file_positions = [i for i, kind in enumerate(kinds) if kind == "file"]
    assert max(dir_positions) < min(file_positions)
    dirs = [f["path"] for f in middle if f["kind"] == "dir"]
    files = [
        (f["path"], f.get("digest"), f.get("revision"))
        for f in middle
        if f["kind"] == "file"
    ]
    assert dirs == EXPECTED_DIRS
    assert files == EXPECTED_FILES
    for frame in middle:
        assert frame["cursor"] == f"{frame['kind']}:{frame['path']}"

    # Every listing was admitted through the single chokepoint, per path;
    # the root is admitted again inside the stream (pre-content admission
    # plus the traversal itself).
    listed = [params["path"] for _op, params, _caller, _surface in calls]
    assert listed == ["", "", "docs", "src", "src/deep"]
    assert all(_surface.name == "HTTP" for _op, _params, _caller, _surface in calls)

    # A resumed fetch continues from the last acknowledged entry.
    resumed = await _fetch(
        app, {"repo": REPOSITORY, "ref": REF, "after": "file:docs/guide.md"}
    )
    assert resumed[0] == 200
    resumed_frames = _frames(resumed[2])
    assert [f["path"] for f in resumed_frames if f["kind"] == "file"] == [
        "src/app.py",
        "src/deep/nested.txt",
    ]
    assert resumed_frames[-1] == {"kind": "end", "complete": True}

    # A cursor that names no streamed entry refuses before content.
    bogus = await _fetch(app, {"repo": REPOSITORY, "ref": REF, "after": "file:missing"})
    bogus_frames = _frames(bogus[2])
    assert bogus_frames[0]["kind"] == "start"
    assert bogus_frames[1] == {
        "kind": "error",
        "code": "INVALID_ARGUMENT",
        "reason": "unknown resume cursor",
    }
    assert len(bogus_frames) == 2


async def test_checkout_denied_top_level_path_refuses_before_content():
    async def fake_invoke(op_id, params, caller, surface, *, services):
        return OpError("SUBJECT_ACCESS_DENIED", {}, source="arbora")

    app = _app(fake_invoke)
    status, content_type, body = await _fetch(app, {"repo": REPOSITORY, "ref": REF})
    assert status == 403
    assert content_type.startswith("application/json")
    envelope = json.loads(body)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "SUBJECT_ACCESS_DENIED"
    assert envelope["error"]["op"] == "source.tree"
    # A refusal is an envelope, never a stream.
    assert "\n" not in body


async def test_checkout_denied_subpath_ends_the_stream():
    async def fake_invoke(op_id, params, caller, surface, *, services):
        if params["path"] == "src":
            return OpError("SCOPE_REQUIRED", {"missing_scopes": ["blob:read"]})
        return _listing(params["path"])

    app = _app(fake_invoke)
    status, content_type, body = await _fetch(app, {"repo": REPOSITORY, "ref": REF})
    assert status == 200  # the admitted root was already admitted
    frames = _frames(body)
    assert frames[0]["kind"] == "start"
    assert frames[1] == {
        "kind": "error",
        "code": "SCOPE_REQUIRED",
        "reason": "a subpath of the checkout is not admitted",
    }
    # The denied subtree never streams, and no end frame follows an error.
    assert [frame["kind"] for frame in frames] == ["start", "error"]


@pytest.mark.parametrize(
    "query",
    [
        {"repo": REPOSITORY},  # no ref
        {"ref": REF},  # no repo
        {"repo": REPOSITORY, "ref": REF, "path": "/absolute"},
        {"repo": REPOSITORY, "ref": REF, "path": "a/../b"},
        {"repo": REPOSITORY, "ref": REF, "after": "bogus"},
    ],
)
async def test_checkout_invalid_request_refuses_before_any_invocation(query):
    async def fake_invoke(op_id, params, caller, surface, *, services):
        raise AssertionError("an invalid request must not reach the chokepoint")

    app = _app(fake_invoke)
    status, content_type, body = await _fetch(app, query)
    assert status == 400
    assert content_type.startswith("application/json")
    assert json.loads(body)["error"]["code"] == "INVALID_ARGUMENT"


async def test_checkout_unauthenticated_refuses():
    async def fake_invoke(op_id, params, caller, surface, *, services):
        raise AssertionError("an unauthenticated request must not reach the chokepoint")

    app = _app(fake_invoke, authenticator=FakeAuthenticator(denied=True))
    status, content_type, body = await _fetch(app, {"repo": REPOSITORY, "ref": REF})
    assert status == 401
    assert content_type.startswith("application/json")
    assert json.loads(body)["error"]["code"] == "UNAUTHENTICATED"
