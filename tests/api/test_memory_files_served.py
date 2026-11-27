"""Versioned memory files served over HTTP against a fake engine (ARBORA-OPS-R024.4)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from arbora.api.ops.memory_files import MemoryFilesSave, digest_matches, sync_outcome
from arbora.observability.state import SubsystemWindows
from tests import test_served_operation_api as served_api
from tests.test_served_operation_api import (  # noqa: F401
    FakeClient,
    _call,
    _value,
    served,
)

pytestmark = pytest.mark.spec("ARBORA-OPS-R024.4")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class _Snapshots:
    """The engine's three memory snapshot methods over one dict."""

    def __init__(self) -> None:
        self.docs: dict[str, list[dict[str, Any]]] = {}
        self.retained: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, Any, Any]] = []
        self.acks: list[tuple[str, Any, Any]] = []

    def head(self, doc: str, text: str, revision: int, parent: int | None) -> dict:
        return {
            "doc_id": doc,
            "revision": revision,
            "parent_revision": parent,
            "content_digest": _digest(text),
            "content_len": len(text.encode()),
            "graph_incarnation": "g1",
            "provenance": {
                "actor": "person:one",
                "policy_digest": "0" * 64,
                "request_id": "r",
            },
        }

    def read(self, params: Any) -> Any:
        doc = params["doc_id"]
        if doc.startswith("denied/") or doc not in self.docs:
            raise PermissionError("MEMSYNC_NOT_AVAILABLE")
        revisions = self.docs[doc]
        wanted = params.get("revision")
        row = revisions[-1] if wanted is None else revisions[wanted - 1]
        return {"content": row["text"], "head": row["head"]}

    def list(self, params: Any) -> Any:
        heads = [
            rows[-1]["head"]
            for doc, rows in sorted(self.docs.items())
            if doc.startswith(params["prefix"])
        ]
        return {"heads": heads[: params["limit"]], "next_cursor": None}

    def sync(self, params: Any) -> Any:
        request = params["request"]
        doc, action = request["doc_id"], request["action"]
        if doc.startswith("denied/"):
            return {"outcome": "refused", "code": "MEMSYNC_NOT_AVAILABLE"}
        rows = self.docs.setdefault(doc, [])
        if action["action"] == "submit":
            head_rev = len(rows) or None
            if action.get("expected_revision") != head_rev:
                revision = len(rows) + 1
                text = action["content"]
                self.retained.setdefault(doc, []).append(
                    {"text": text, "revision": revision}
                )
                return {
                    "outcome": "conflict_retained",
                    "candidate_revision": revision,
                    "head": rows[-1]["head"],
                }
            text = action["content"]
            head = self.head(doc, text, len(rows) + 1, head_rev)
            rows.append({"text": text, "head": head})
            return {"outcome": "accepted", "head": head}
        if action["action"] == "acknowledge":
            self.acks.append((doc, action, request["request_id"]))
            return {"outcome": "acknowledged", "state": "in_sync"}
        candidate = self.retained[doc].pop()
        if action["resolution"]["resolution"] == "take_candidate":
            head = self.head(doc, candidate["text"], len(rows) + 1, len(rows))
            rows.append({"text": candidate["text"], "head": head})
        return {"outcome": "resolved", "head": rows[-1]["head"]}


@pytest.fixture
def files(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Any:

    composed: list[Any] = []
    compose = served_api.compose_served_invocation

    def capture(**kwargs: Any) -> Any:
        composed.append(compose(**kwargs))
        return composed[-1]

    monkeypatch.setattr(served_api, "compose_served_invocation", capture)

    monkeypatch.setenv("MEMORY_FILES_ROOT", str(str(tmp_path / "root")))
    monkeypatch.setattr(
        served_api, "SCOPES", (*served_api.SCOPES, "memory:read", "memory:write")
    )
    app, client = request.getfixturevalue("served")
    snapshots = _Snapshots()
    original = FakeClient._send

    async def send(self: FakeClient, method: str, params: Any, graph: Any, **kw: Any):
        handlers = {
            "ReadMemorySnapshot": snapshots.read,
            "ListMemorySnapshots": snapshots.list,
            "SyncMemorySnapshot": snapshots.sync,
        }
        if method in handlers:
            snapshots.calls.append((method, params, kw.get("idempotency_key")))
            return handlers[method](params)
        return await original(self, method, params, graph, **kw)

    monkeypatch.setattr(FakeClient, "_send", send)
    return app, snapshots, composed


def _save(app: Any, doc: str, text: str, key: str, **extra: Any) -> Any:
    body = {"doc_id": doc, "content": text, "content_digest": _digest(text), **extra}
    return _call(app, "memory.files.save", body, **{"Idempotency-Key": key})


@pytest.mark.spec("ARBORA-OPS-R024.4.1")
def test_models_refuse_invalid_fields_and_digest_mismatch() -> None:
    with pytest.raises(ValueError):
        MemoryFilesSave(doc_id="d", content="x")  # no digest
    with pytest.raises(ValueError):
        MemoryFilesSave.model_validate(
            {"doc_id": "d", "content": "x", "content_digest": "nothex", "extra": 1}
        )
    assert digest_matches("x", _digest("x"))
    assert not digest_matches("x", _digest("y"))
    assert sync_outcome({"outcome": "refused", "code": "MEMSYNC_NOT_AVAILABLE"}) == {
        "outcome": "refused"
    }


@pytest.mark.spec("ARBORA-OPS-R024.4.2", "ARBORA-OPS-R024.4.3", "ARBORA-OPS-R024.4.6")
def test_complete_served_memory_file_journey(files: Any) -> None:
    app, snapshots, _ = files
    first = _value(_save(app, "notes/a", "one", "k1"))
    assert first["outcome"] == "accepted"
    assert first["head"]["revision"] == 1
    read = _value(_call(app, "memory.files.read", {"doc_id": "notes/a"}))
    assert read["content"] == "one"
    stale = _value(_save(app, "notes/a", "two", "k2"))
    assert stale["outcome"] == "conflict_retained"
    assert stale["head"]["revision"] == 1
    resolved = _value(
        _save_resolution(app, "notes/a", stale["candidate_revision"], "k3")
    )
    assert resolved["outcome"] == "resolved"
    listed = _value(_call(app, "memory.files.list", {"prefix": "notes/"}))
    assert [h["doc_id"] for h in listed["heads"]] == ["notes/a"]
    assert listed["next_cursor"] is None
    saves = [c for c in snapshots.calls if c[0] == "SyncMemorySnapshot"]
    assert [c[2] for c in saves if not c[2].endswith(":ack")] == ["k1", "k2", "k3"]


def _save_resolution(app: Any, doc: str, candidate: int, key: str) -> Any:
    body = {
        "action": "resolve",
        "doc_id": doc,
        "candidate_revision": candidate,
        "expected_head_revision": 1,
        "resolution": "take_candidate",
    }
    return _call(app, "memory.files.save", body, **{"Idempotency-Key": key})


@pytest.mark.spec("ARBORA-OPS-R024.4.3")
def test_digest_mismatch_is_refused_before_dispatch(files: Any) -> None:
    app, snapshots, _ = files
    response = _call(
        app,
        "memory.files.save",
        {"doc_id": "d", "content": "x", "content_digest": _digest("y")},
        **{"Idempotency-Key": "k"},
    )
    assert response.status_code == 400
    assert snapshots.calls == []


@pytest.mark.spec("ARBORA-OPS-R024.4.5")
def test_a_denied_save_is_redacted_and_carries_no_head(files: Any) -> None:
    app, _, _ = files
    denied = _value(_save(app, "denied/x", "one", "k1"))
    assert denied == {"outcome": "refused"}


@pytest.mark.spec("ARBORA-OPS-R024.4.4")
def test_materialization_writes_the_file_then_acknowledges_with_a_growing_generation(
    files: Any, tmp_path: Path
) -> None:
    app, snapshots, _ = files
    first = _value(_save(app, "notes/a", "one", "k1"))
    assert first["materialized"] is True
    assert (tmp_path / "root/notes/a").read_text() == "one"
    (_, ack, request_id) = snapshots.acks[0]
    assert ack["revision"] == 1
    assert ack["content_digest"] == _digest("one")
    assert request_id == "k1:ack"
    second = _value(_save(app, "notes/a", "two", "k2", expected_revision=1))
    assert second["head"]["revision"] == 2
    assert (tmp_path / "root/notes/a").read_text() == "two"
    assert (
        snapshots.acks[1][1]["observation_generation"] > ack["observation_generation"]
    )


@pytest.mark.spec("ARBORA-OPS-R024.4.4")
def test_an_unset_root_refuses_before_any_engine_call(
    files: Any, monkeypatch: pytest.MonkeyPatch
) -> None:

    app, snapshots, _ = files
    monkeypatch.delenv("MEMORY_FILES_ROOT", raising=False)
    response = _save(app, "notes/a", "one", "k1")
    assert response.status_code == 503
    assert snapshots.calls == []


@pytest.mark.spec("ARBORA-OPS-R024.4.4")
def test_a_path_outside_the_root_is_refused_before_any_engine_call(files: Any) -> None:
    app, snapshots, _ = files
    for doc in ("../escape", "/abs", "a//b", "a/./b"):
        assert _save(app, doc, "x", f"k-{doc}").status_code == 400
    assert snapshots.calls == []


@pytest.mark.spec("ARBORA-OPS-R024.4.4")
def test_a_failed_file_write_sends_no_acknowledgment(
    files: Any, tmp_path: Path
) -> None:
    app, snapshots, _ = files
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes").write_text("a file where the directory must go")
    outcome = _value(_save(app, "notes/a", "one", "k1"))
    assert outcome["outcome"] == "accepted"
    assert outcome["materialized"] is False
    assert snapshots.acks == []


@pytest.mark.spec("ARBORA-OPS-R024.4.6")
@pytest.mark.asyncio
async def test_the_journey_over_mcp(files: Any, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from starlette.requests import Request

    from arbora.api.invoke import invoke
    from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
    from arbora.api.policy import PolicyGate

    _app, snapshots, composed = files
    invocation = composed[0]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer fixture-verified")],
    }
    caller = await invocation.authenticator.authenticate(Request(scope))
    projection = MCPProjection(
        registry=invocation.services.registry,
        services=invocation.services,
        resolver=SimpleNamespace(scope_ref=lambda *a, **k: "scope"),
        caller_for_request=lambda: caller,
        policy_gate=PolicyGate(mode="none"),
        invoke=invoke,
        windows=SubsystemWindows(),
    )
    body = {"doc_id": "notes/m", "content": "one", "content_digest": _digest("one")}
    saved = await dispatch_verb(
        "act",
        projection,
        op="memory.files.save",
        params=body,
        idempotency_key="m1",
    )
    assert saved["ok"] is True, saved
    assert saved["result"]["value"]["outcome"] == "accepted"
    assert (tmp_path / "root/notes/m").read_text() == "one"
    read = await dispatch_verb(
        "ask", projection, op="memory.files.read", params={"doc_id": "notes/m"}
    )
    assert read["result"]["value"]["content"] == "one"
    listed = await dispatch_verb(
        "ask", projection, op="memory.files.list", params={"prefix": "notes/"}
    )
    assert [h["doc_id"] for h in listed["result"]["value"]["heads"]] == ["notes/m"]
    assert snapshots.acks[-1][2] == "m1:ack"
