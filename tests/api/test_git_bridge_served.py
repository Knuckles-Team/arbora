"""The git smart-HTTP read bridge over the served path (ARBORA-OPS-R851).

The app is the real served composition: each request authenticates and drives the
``source.git.*`` operations through the invoke chokepoint to a recording engine that
holds the refs, exports one pack and keeps its bytes in the blob store. The git
methods are engine ``main`` shapes, so the double declares them.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from arbora.core.settings import runtime_config
from tests.api import test_bundle_ops_served as base
from tests.api.test_bundle_ops_served import _call

HEAD = "a" * 40
OLD = "b" * 40
PACK = b"PACK" + b"\x00\x00\x00\x02" + b"x" * 1_048_576 + b"tail"
DIGEST = hashlib.sha256(PACK).hexdigest()


class GitEngine(base._Engine):
    """Refs of one repository, one exported pack, and a denied path."""

    denied = False

    def _m_GitRefsList(self, p: dict[str, Any]) -> Any:
        assert list(p) == ["request"] and p["request"]["repo"] == "repo"
        return {
            "refs": [
                {"ref": "refs/heads/main", "object_id": HEAD},
                {"ref": "refs/heads/old", "object_id": OLD},
            ]
        }

    def _m_GitExportPack(self, p: dict[str, Any]) -> Any:
        from medulla import EngineResponseError

        if self.denied:
            raise EngineResponseError("PATH_READ_DENIED", "a path is outside the grant")
        self.store.blobs[(self.tenant, DIGEST)] = PACK
        return {
            "object_format": "sha1",
            "pack_ref": f"cas:sha256:{DIGEST}",
            "pack_bytes": len(PACK),
        }

    def _m_BlobChunkGet(self, p: dict[str, Any]) -> Any:
        blob = self.store.blobs[(self.tenant, DIGEST)]
        return blob[p["idx"] * 1_048_576 : (p["idx"] + 1) * 1_048_576]


def _declare(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated
    from medulla.generated import _runtime
    from medulla.generated import models as generated_models

    declared = frozenset(generated.METHOD_IDS) | {"GitRefsList", "GitExportPack"}
    monkeypatch.setattr(generated, "METHOD_IDS", declared)
    monkeypatch.setattr(_runtime, "METHOD_IDS", declared)
    for name in ("GitRefsListRequest", "GitExportPackRequest", "GitExportPackReceipt"):
        monkeypatch.setattr(generated_models, name, object, raising=False)


SOURCE_READ = ("node:read", "edge:read", "blob:read")


def _served(
    monkeypatch: pytest.MonkeyPatch, roles: tuple[str, ...] = SOURCE_READ
) -> tuple[Any, GitEngine]:
    monkeypatch.setattr(base, "_Engine", GitEngine)
    _declare(monkeypatch)
    app, engine = base._app(monkeypatch, base._Store(), roles)
    return app, engine  # type: ignore[return-value]


def _pkt(text: str) -> bytes:
    return b"%04x" % (len(text) + 4) + text.encode()


def _upload(*lines: str) -> bytes:
    return b"".join(_pkt(line) for line in lines)


@pytest.mark.spec("ARBORA-OPS-R851")
def test_clone_fetch_and_denied_path_over_smart_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, engine = _served(monkeypatch)
    # Discovery: the refs advertise HEAD, then every ref, under one symref capability.
    advert = _call(app, "GET", "/api/v1/git/repo/info/refs?service=git-upload-pack")
    assert advert.status_code == 200, advert.text
    assert advert.headers["content-type"].startswith(
        "application/x-git-upload-pack-advertisement"
    )
    body = advert.content
    assert body.startswith(_pkt("# service=git-upload-pack\n") + b"0000")
    assert f"{HEAD} HEAD\0".encode() in body
    assert b"symref=HEAD:refs/heads/main" in body
    assert f"{HEAD} refs/heads/main\n".encode() in body and body.endswith(b"0000")
    # Clone: the wants and done answer NAK, then the engine's pack bytes, unchanged.
    clone = _call(
        app,
        "POST",
        "/api/v1/git/repo/git-upload-pack",
        content=_upload(f"want {HEAD} agent=git\n") + b"0000" + _upload("done\n"),
    )
    assert clone.status_code == 200, clone.text
    assert clone.content == _pkt("NAK\n") + PACK
    export = [p for m, p, _g in engine.sent if m == "GitExportPack"][-1]
    assert export["request"] == {
        "repo": "repo",
        "branch": "main",
        "wants": [HEAD],
        "haves": [],
    }
    # Fetch: a have is sent on, and a body without done negotiates with NAK alone.
    fetch = _call(
        app,
        "POST",
        "/api/v1/git/repo/git-upload-pack",
        content=_upload(f"want {HEAD}\n") + b"0000" + _upload(f"have {OLD}\n"),
    )
    assert fetch.content == _pkt("NAK\n")
    again = _call(
        app,
        "POST",
        "/api/v1/git/repo/git-upload-pack",
        content=_upload(f"want {HEAD}\n")
        + b"0000"
        + _upload(f"have {OLD}\n", "done\n"),
    )
    assert again.content == _pkt("NAK\n") + PACK
    export = [p for m, p, _g in engine.sent if m == "GitExportPack"][-1]
    assert export["request"]["haves"] == [OLD]
    # A denied path refuses the whole request with 403 and sends no pack byte.
    engine.denied = True
    denied = _call(
        app,
        "POST",
        "/api/v1/git/repo/git-upload-pack",
        content=_upload(f"want {HEAD}\n") + b"0000" + _upload("done\n"),
    )
    assert denied.status_code == 403
    assert denied.headers["content-type"].startswith("application/json")
    assert PACK[:4] not in denied.content


@pytest.mark.spec("ARBORA-OPS-R851")
def test_push_is_not_served_and_an_unknown_want_makes_no_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, engine = _served(monkeypatch)
    push = _call(app, "POST", "/api/v1/git/repo/git-receive-pack", content=b"0000")
    advertised = _call(
        app, "GET", "/api/v1/git/repo/info/refs?service=git-receive-pack"
    )
    for answer in (push, advertised):
        assert answer.status_code == 403
        assert answer.json()["error"]["reason"] == "push_not_served"
    unknown = _call(
        app,
        "POST",
        "/api/v1/git/repo/git-upload-pack",
        content=_upload(f"want {'c' * 40}\n") + b"0000" + _upload("done\n"),
    )
    assert unknown.status_code == 400
    assert not [m for m, _p, _g in engine.sent if m == "GitExportPack"]


@pytest.mark.spec("ARBORA-OPS-R851")
def test_a_pack_over_the_configured_cap_is_refused_before_any_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _engine = _served(monkeypatch)
    monkeypatch.setattr(runtime_config, "git_bridge_max_pack_bytes", 1_048_576)
    answer = _call(
        app,
        "POST",
        "/api/v1/git/repo/git-upload-pack",
        content=_upload(f"want {HEAD}\n") + b"0000" + _upload("done\n"),
    )
    assert answer.status_code == 409, answer.text
    assert answer.json()["error"]["code"] == "LOAD_CAP_EXCEEDED"


@pytest.mark.spec("ARBORA-OPS-R851")
def test_a_caller_without_the_source_read_scopes_reads_no_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, engine = _served(monkeypatch, roles=("graph:read",))
    answer = _call(app, "GET", "/api/v1/git/repo/info/refs?service=git-upload-pack")
    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "SCOPE_REQUIRED"
    assert not [m for m, _p, _g in engine.sent if m.startswith("Git")]
