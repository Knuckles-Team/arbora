"""Native source reader: admitted engine reads under the caller's authority.

No installed-engine acceptance: the projection is a canned branch-aware
source model served through a recording fake of the pinned wire contract,
so the tests prove the admission order, the per-method scope pre-checks
and the projection the reader serves (directories derived from file
paths, file-level last-touch, deleted refs excluded). The fake replaces
``send_by_id`` only, so the reader's real ``_send`` -- verified-graph
dispatch, per-request claims binding and engine-refusal conversion -- is
exercised on every call.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace

import msgpack
import pytest
from epistemic_graph import EngineResponseError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import VerifiedCaller
from arbora.mcp_server import source_reader as reader_module
from arbora.mcp_server.source_reader import NativeSourceReader
from tests._support import Actor

pytestmark = pytest.mark.spec("ARBORA-OPS-R848.1")

REPOSITORY = "repo-a"
BRANCH_MAIN = "branch:main"
BRANCH_DEV = "branch:develop"
BRANCH_DEAD = "branch:dead"
BRANCH_FOREIGN = "branch:foreign"

BRANCHES: list[tuple[str, dict]] = [
    (
        BRANCH_MAIN,
        {
            "repository_id": REPOSITORY,
            "ref_name": "main",
            "revision_id": "rev:main",
            "status": "live",
            "type": "Branch",
        },
    ),
    (
        BRANCH_DEV,
        {
            "repository_id": REPOSITORY,
            "ref_name": "develop",
            "revision_id": "rev:develop",
            "status": "live",
            "type": "Branch",
        },
    ),
    (
        BRANCH_DEAD,
        {
            "repository_id": REPOSITORY,
            "ref_name": "dead",
            "revision_id": "rev:dead",
            "status": "deleted",
            "type": "Branch",
        },
    ),
    (
        BRANCH_FOREIGN,
        {
            "repository_id": "repo-b",
            "ref_name": "main",
            "revision_id": "rev:foreign",
            "status": "live",
            "type": "Branch",
        },
    ),
]

VERSIONS: dict[str, dict[str, dict]] = {
    BRANCH_MAIN: {
        "fv:readme": {
            "path": "README.md",
            "content_digest": "digest:readme",
            "type": "FileVersion",
        },
        "fv:app": {
            "path": "src/app.py",
            "content_digest": "digest:app",
            "type": "FileVersion",
        },
        "fv:deep": {
            "path": "src/deep/nested.txt",
            "content_digest": "digest:deep",
            "type": "FileVersion",
        },
        "fv:guide": {
            "path": "docs/guide.md",
            "content_digest": "digest:guide",
            "type": "FileVersion",
        },
    },
    BRANCH_DEV: {
        "fv:app-dev": {
            "path": "src/app.py",
            "content_digest": "digest:app-dev",
            "type": "FileVersion",
        },
    },
    BRANCH_FOREIGN: {
        "fv:foreign": {
            "path": "src/app.py",
            "content_digest": "digest:foreign",
            "type": "FileVersion",
        },
    },
}

EDGES: dict[tuple[str, str], dict] = {
    (BRANCH_MAIN, "fv:readme"): {
        "relationship": "hasFileVersion",
        "revision_id": "rev:101",
        "evidence_rung": "EXTRACTED",
    },
    (BRANCH_MAIN, "fv:app"): {
        "relationship": "hasFileVersion",
        "revision_id": "rev:102",
        "evidence_rung": "EXTRACTED",
    },
    (BRANCH_MAIN, "fv:deep"): {
        "relationship": "hasFileVersion",
        "revision_id": "rev:103",
        "evidence_rung": "EXTRACTED",
    },
    (BRANCH_MAIN, "fv:guide"): {
        "relationship": "hasFileVersion",
        "revision_id": "rev:104",
        "evidence_rung": "EXTRACTED",
    },
    (BRANCH_DEV, "fv:app-dev"): {
        "relationship": "hasFileVersion",
        "revision_id": "rev:201",
        "evidence_rung": "EXTRACTED",
    },
    (BRANCH_FOREIGN, "fv:foreign"): {
        "relationship": "hasFileVersion",
        "revision_id": "rev:900",
        "evidence_rung": "EXTRACTED",
    },
}

BLOBS: dict[str, list[bytes]] = {
    "digest:app": [b"def main():\n", b"    return 1\n"],
}


def _blob(props: dict) -> bytes:
    return msgpack.packb(props, use_bin_type=True)


class Client:
    def __init__(self) -> None:
        self.claims = ContextVar("source-reader-test-claims", default=None)

    @contextlib.contextmanager
    def use_verified_context(self, claims):
        token = self.claims.set(claims)
        try:
            yield self
        finally:
            self.claims.reset(token)


class Engine:
    """One canned projection served through the pinned wire shapes."""

    def __init__(self) -> None:
        self.branches = list(BRANCHES)
        self.versions = {branch: dict(rows) for branch, rows in VERSIONS.items()}
        self.edges = dict(EDGES)
        self.blobs = {digest: list(chunks) for digest, chunks in BLOBS.items()}
        self.calls: list[tuple[str, dict, str, object]] = []
        self.full_pages = False
        self.error: Exception | None = None

    async def send_by_id(self, client, method, params=None, graph=None, **_kwargs):
        params = dict(params or {})
        self.calls.append((method, params, graph, client.claims.get()))
        if graph != "control":
            raise AssertionError(f"engine read left the verified graph: {graph}")
        if self.error is not None:
            raise self.error
        if method == "GetNodesByLabel":
            assert params["label"] == "Branch"
            assert params["limit"] == reader_module._PAGE_SIZE
            if self.full_pages:
                # A scan that never sees a short page: the answer is unknown.
                return [[BRANCH_MAIN, dict(BRANCHES[0][1])]] * reader_module._PAGE_SIZE
            rows = [[node_id, dict(props)] for node_id, props in self.branches]
            after = params.get("after")
            if after is not None:
                start = next(i for i, row in enumerate(rows) if row[0] == after) + 1
                rows = rows[start:]
            return rows[: reader_module._PAGE_SIZE]
        if method == "GetSuccessors":
            return sorted(self.versions.get(params["node_id"], {}))
        if method == "GetNodePropertiesBatch":
            out = []
            for node_id in params["node_ids"]:
                props = self._find_version(node_id)
                out.append([node_id, _blob(props) if props is not None else None])
            return out
        if method == "GetEdgePropertiesBatch":
            out = []
            for source, target in params["edges"]:
                props = self.edges.get((source, target))
                out.append([_blob(props)] if props is not None else [])
            return out
        if method == "BlobFetchBegin":
            chunks = self.blobs[params["digest"]]
            return [4242, len(chunks)]
        if method == "BlobChunkGet":
            chunks = self.blobs["digest:app"]
            assert params["cursor"] == 4242
            return chunks[params["idx"]]
        if method == "BlobFetchEnd":
            assert params["cursor"] == 4242
            return True
        raise AssertionError(f"unexpected engine method: {method}")

    def _find_version(self, node_id: str) -> dict | None:
        for rows in self.versions.values():
            if node_id in rows:
                return rows[node_id]
        return None


def _patch_engine(monkeypatch, engine) -> None:
    import epistemic_graph.generated as generated

    monkeypatch.setattr(generated, "send_by_id", engine.send_by_id)


def _forbidden_engine(monkeypatch) -> None:
    import epistemic_graph.generated as generated

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid authority reached engine")

    monkeypatch.setattr(generated, "send_by_id", forbidden)


def context(principal="caller-a", tenant="tenant-a", client=None, scopes=None):
    scopes = ["node:read", "edge:read", "blob:read"] if scopes is None else scopes
    claims = {
        "principal": principal,
        "tenant": tenant,
        "scopes": list(scopes),
        "policy_version": "v1",
        "delegation": [],
    }
    actor = Actor(actor_id=principal, tenant_id=tenant)
    session = SimpleNamespace(
        actor=actor,
        tenant=tenant,
        graph="control",
        engine_verified_context=lambda: claims,
    )
    caller = VerifiedCaller(
        principal=principal,
        tenant=tenant,
        effective_scopes=frozenset(scopes),
        engine_claims=claims,
        principal_kind="human",
        authenticated=True,
        delegated=False,
        credential_kind="session",
        policy_revision="v1",
        session=session,
    )
    return ExecutionContext(
        caller=caller,
        client=client or Client(),
        owner=principal,
        service_identity=False,
        services={"source_reader": NativeSourceReader()},
    )


def methods(engine: Engine) -> list[str]:
    return [method for method, _params, _graph, _claims in engine.calls]


def test_native_reader_satisfies_the_typed_port():
    from arbora.api.ops.source import SourceReader

    reader = NativeSourceReader()
    assert isinstance(reader, SourceReader)
    assert vars(reader) == {}


async def test_tree_lists_derived_directories_and_files_in_path_order(monkeypatch):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()

    outcome = await reader.tree(repository=REPOSITORY, ref="main", path="", context=ctx)
    assert outcome.found is True
    assert [entry.path for entry in outcome.entries] == ["README.md", "docs", "src"]
    readme = outcome.entries[0]
    assert (
        readme.kind,
        readme.content_digest,
        readme.revision_id,
    ) == ("file", "digest:readme", "rev:101")
    assert (outcome.entries[1].kind, outcome.entries[1].content_digest) == (
        "directory",
        None,
    )

    src = await reader.tree(repository=REPOSITORY, ref="main", path="src", context=ctx)
    assert [entry.path for entry in src.entries] == ["src/app.py", "src/deep"]
    assert src.entries[0].revision_id == "rev:102"

    deep = await reader.tree(
        repository=REPOSITORY, ref="main", path="src/deep", context=ctx
    )
    assert [entry.path for entry in deep.entries] == ["src/deep/nested.txt"]
    # A file path is not a directory: the listing is admitted but empty.
    flat = await reader.tree(
        repository=REPOSITORY, ref="main", path="README.md", context=ctx
    )
    assert flat.found is True and flat.entries == ()
    # The read never touches the blob storage plane.
    assert not any(method.startswith("Blob") for method in methods(engine))
    # Every engine read ran under the admitted caller's own claims.
    for _method, _params, _graph, claims in engine.calls:
        assert claims is not None and claims["principal"] == "caller-a"
        assert claims["scopes"] == ["node:read", "edge:read", "blob:read"]
    assert ctx.client.claims.get() is None


async def test_tree_ref_not_found(monkeypatch):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    outcome = await reader.tree(
        repository=REPOSITORY, ref="ghost", path="", context=ctx
    )
    assert outcome.found is False and outcome.reason == "ref-not-found"
    assert outcome.entries == () and outcome.tenant == "tenant-a"
    # A deleted ref is not served even when it names a live-looking path.
    dead = await reader.tree(repository=REPOSITORY, ref="dead", path="", context=ctx)
    assert dead.found is False and dead.reason == "ref-not-found"


async def test_foreign_repository_is_not_served(monkeypatch):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    # repo-b's own ref is served...
    own = await reader.tree(repository="repo-b", ref="main", path="src", context=ctx)
    assert [entry.path for entry in own.entries] == ["src/app.py"]
    # ...but repo-a's reads never see repo-b's branches or files.
    history = await reader.history(
        repository=REPOSITORY, path="src/app.py", context=ctx
    )
    assert [entry.ref for entry in history.entries] == ["develop", "main"]
    assert all(entry.content_digest != "digest:foreign" for entry in history.entries)


async def test_blob_streams_chunks_and_ends_fetch(monkeypatch):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    outcome = await reader.blob(
        repository=REPOSITORY, ref="main", path="src/app.py", context=ctx
    )
    assert outcome.found is True
    assert outcome.content == b"def main():\n    return 1\n"
    assert outcome.content_digest == "digest:app"
    assert outcome.size == 25
    assert methods(engine) == [
        "GetNodesByLabel",
        "GetSuccessors",
        "GetNodePropertiesBatch",
        "BlobFetchBegin",
        "BlobChunkGet",
        "BlobChunkGet",
        "BlobFetchEnd",
    ]
    assert engine.calls[4][1] == {"cursor": 4242, "idx": 0}
    assert engine.calls[5][1] == {"cursor": 4242, "idx": 1}
    assert engine.calls[6][1] == {"cursor": 4242}
    assert ctx.client.claims.get() is None


async def test_blob_fetch_end_released_on_chunk_refusal(monkeypatch):
    engine = Engine()

    async def send_by_id(client, method, params=None, graph=None, **_kwargs):
        if method == "BlobChunkGet":
            raise EngineResponseError("ACCESS_DENIED", "private backend detail")
        return await engine.send_by_id(client, method, params, graph, **_kwargs)

    import epistemic_graph.generated as generated

    monkeypatch.setattr(generated, "send_by_id", send_by_id)
    reader = NativeSourceReader()
    ctx = context()
    with pytest.raises(EngineRefusal) as caught:
        await reader.blob(
            repository=REPOSITORY, ref="main", path="src/app.py", context=ctx
        )
    assert caught.value.code == "ACCESS_DENIED"
    # The open fetch is released even though the chunk refused.
    assert methods(engine)[-1] == "BlobFetchEnd"
    assert ctx.client.claims.get() is None


async def test_blob_path_not_found(monkeypatch):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    outcome = await reader.blob(
        repository=REPOSITORY, ref="main", path="nope.py", context=ctx
    )
    assert outcome.found is False and outcome.reason == "path-not-found"
    assert outcome.content == b"" and outcome.content_digest is None
    assert not any(method.startswith("Blob") for method in methods(engine))


async def test_history_lists_every_ref_carrying_the_path(monkeypatch):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    outcome = await reader.history(
        repository=REPOSITORY, path="src/app.py", context=ctx
    )
    assert [
        (entry.ref, entry.revision_id, entry.content_digest)
        for entry in outcome.entries
    ] == [
        ("develop", "rev:201", "digest:app-dev"),
        ("main", "rev:102", "digest:app"),
    ]
    # A path no live ref carries answers with an empty history, not a refusal.
    empty = await reader.history(repository=REPOSITORY, path="missing.txt", context=ctx)
    assert empty.entries == ()


async def test_blame_serves_file_level_last_touch(monkeypatch):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    outcome = await reader.blame(
        repository=REPOSITORY, ref="main", path="src/app.py", context=ctx
    )
    assert outcome.found is True
    assert outcome.revision_id == "rev:102"
    assert outcome.content_digest == "digest:app"
    assert outcome.line_level is False
    absent = await reader.blame(
        repository=REPOSITORY, ref="main", path="nope.py", context=ctx
    )
    assert absent.found is False and absent.reason == "path-not-found"
    assert absent.revision_id is None and absent.line_level is False


async def test_branch_scan_limit_refuses_instead_of_answering_empty(monkeypatch):
    engine = Engine()
    engine.full_pages = True
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    with pytest.raises(OperationRefused) as caught:
        await reader.tree(repository=REPOSITORY, ref="main", path="", context=ctx)
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.details == {"reason": "branch scan limit exceeded"}
    assert len([c for c in engine.calls if c[0] == "GetNodesByLabel"]) == (
        reader_module._MAX_SCAN_PAGES
    )


async def test_malformed_engine_result_refuses(monkeypatch):
    import epistemic_graph.generated as generated

    async def junk(client, method, params=None, graph=None, **_kwargs):
        return {"not": "a list"}

    monkeypatch.setattr(generated, "send_by_id", junk)
    reader = NativeSourceReader()
    ctx = context()
    with pytest.raises(OperationRefused) as caught:
        await reader.tree(repository=REPOSITORY, ref="main", path="", context=ctx)
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.details == {"reason": "engine result malformed"}


async def test_engine_refusal_keeps_its_engine_code(monkeypatch):
    engine = Engine()
    engine.error = EngineResponseError("ACCESS_DENIED", "private backend detail")
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context()
    with pytest.raises(EngineRefusal) as caught:
        await reader.tree(repository=REPOSITORY, ref="main", path="", context=ctx)
    assert caught.value.code == "ACCESS_DENIED"
    assert ctx.client.claims.get() is None


@pytest.mark.parametrize(
    ("case", "scopes"),
    [
        ("blob", ["node:read"]),
        ("tree", ["blob:read"]),
    ],
)
async def test_missing_scope_refuses_before_any_engine_read(monkeypatch, case, scopes):
    engine = Engine()
    _patch_engine(monkeypatch, engine)
    reader = NativeSourceReader()
    ctx = context(scopes=scopes)
    with pytest.raises(OperationRefused) as caught:
        await getattr(reader, case)(
            repository=REPOSITORY, ref="main", path="src/app.py", context=ctx
        )
    assert caught.value.code == "SCOPE_REQUIRED"
    assert engine.calls == []


@pytest.mark.parametrize(
    "case",
    ["service", "owner", "tenant", "principal", "expired", "graph", "claims"],
)
async def test_invalid_authority_never_reaches_engine(monkeypatch, case):
    ctx = context()
    if case == "service":
        ctx = replace(ctx, service_identity=True)
    elif case == "owner":
        ctx = replace(ctx, owner="foreign")
    elif case == "tenant":
        ctx.caller.session.tenant = "foreign"
    elif case == "principal":
        ctx.caller.session.actor.actor_id = "foreign"
    elif case == "expired":
        ctx.caller.session.actor.current = False
    elif case == "graph":
        ctx.caller.session.graph = " control "
    else:
        ctx.caller.session.engine_verified_context = lambda: {"principal": "changed"}

    _forbidden_engine(monkeypatch)
    reader = NativeSourceReader()
    with pytest.raises(OperationRefused):
        await reader.tree(repository=REPOSITORY, ref="main", path="", context=ctx)


def test_actual_serving_root_composes_stateless_source_reader(monkeypatch):
    from arbora.a2a import mcp as a2a_mcp
    from arbora.core.settings import runtime_config
    from arbora.mcp_server import served_invocation, server
    from arbora.mcp_server.backing import server_factory

    captured = {}
    monkeypatch.setattr(
        runtime_config, "graph_service_endpoints", ["unix:///test-only"]
    )
    parser = SimpleNamespace(
        parse_known_args=lambda: (SimpleNamespace(transport="stdio"), [])
    )
    monkeypatch.setattr(server_factory, "create_mcp_parser", lambda: parser)
    monkeypatch.setattr(
        server.runtime, "_mint_process_session", lambda transport: object()
    )
    monkeypatch.setattr(a2a_mcp, "_service", lambda: object())

    def compose(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(served_invocation, "served_invocation_from_settings", compose)
    invocation, session = server._served_invocation()
    assert invocation is not None and session is not None
    assert isinstance(captured["bindings"]["source_reader"], NativeSourceReader)
    assert vars(captured["bindings"]["source_reader"]) == {}
