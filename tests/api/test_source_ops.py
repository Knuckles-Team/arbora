"""Served source read operations: one typed read op per engine read.

Round-trips ``source.tree``, ``source.blob``, ``source.history`` and
``source.blame`` through the real invocation chokepoint (ARBORA-OPS-R848.1):
admission is per principal (the exact engine scopes the read uses) and the
typed ``SourceReader`` port is the only path to the engine projection. An
uncomposed reader fails closed with a typed ``UNAVAILABLE``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from typing import Any

import pytest

from arbora.api.invoke import OpError, OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.ops import source as source_ops
from arbora.api.registry import (
    Composite,
    Effect,
    Idempotency,
    Registry,
    Surface,
    Verb,
)
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import setup_services

pytestmark = pytest.mark.spec("ARBORA-OPS-R848.1")

FULL_SCOPES = frozenset({"node:read", "edge:read", "blob:read"})
caller = make_verified_caller("node:read")

#: The exact engine reads each operation makes; scopes are the authz actions
#: of the pinned engine contract (``contract/methods.json``).
EXPECTED_SCOPES = {
    "source.tree": frozenset({"node:read", "edge:read"}),
    "source.blob": FULL_SCOPES,
    "source.history": frozenset({"node:read", "edge:read"}),
    "source.blame": frozenset({"node:read", "edge:read"}),
}
PARAMS = {
    "source.tree": {"repository": "repo-a", "ref": "main", "path": "src"},
    "source.blob": {"repository": "repo-a", "ref": "main", "path": "src/app.py"},
    "source.history": {"repository": "repo-a", "path": "src/app.py"},
    "source.blame": {"repository": "repo-a", "ref": "main", "path": "src/app.py"},
}


class _ClaimsClient:
    """One shared fake client that records the bound per-request claims."""

    def __init__(self) -> None:
        self.claims: dict[str, Any] | None = None

    @contextlib.contextmanager
    def use_verified_context(self, claims: dict[str, Any]):
        self.claims = claims
        try:
            yield self
        finally:
            self.claims = None


class FakeReader:
    """A recorded ``SourceReader`` double: canned outcomes, no engine."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.seen_claims: list[Any] = []
        self.blob_content = b"print('hello')\n"

    def _record(self, method: str, context: Any, **fields: str) -> None:
        self.calls.append((method, fields))
        self.seen_claims.append(context.client.claims)

    async def tree(self, *, repository: str, ref: str, path: str, context: Any):
        self._record("tree", context, repository=repository, ref=ref, path=path)
        return source_ops.SourceTreeOutcome(
            tenant=context.caller.tenant,
            repository=repository,
            ref=ref,
            path=path,
            found=True,
            entries=(
                source_ops.SourceTreeEntry(
                    path="src/app.py",
                    kind="file",
                    content_digest="digest:one",
                    revision_id="rev:one",
                ),
            ),
        )

    async def blob(self, *, repository: str, ref: str, path: str, context: Any):
        self._record("blob", context, repository=repository, ref=ref, path=path)
        content = self.blob_content
        return source_ops.SourceBlobOutcome(
            tenant=context.caller.tenant,
            repository=repository,
            ref=ref,
            path=path,
            found=True,
            content_digest="digest:one",
            content_sha256=hashlib.sha256(content).hexdigest(),
            content=content,
            size=len(content),
        )

    async def history(self, *, repository: str, path: str, context: Any):
        self._record("history", context, repository=repository, path=path)
        return source_ops.SourceHistoryOutcome(
            tenant=context.caller.tenant,
            repository=repository,
            path=path,
            entries=(
                source_ops.SourceHistoryEntry(
                    ref="main",
                    revision_id="rev:one",
                    content_digest="digest:one",
                ),
            ),
        )

    async def blame(self, *, repository: str, ref: str, path: str, context: Any):
        self._record("blame", context, repository=repository, ref=ref, path=path)
        return source_ops.SourceBlameOutcome(
            tenant=context.caller.tenant,
            repository=repository,
            ref=ref,
            path=path,
            found=True,
            revision_id="rev:one",
            content_digest="digest:one",
        )


def _services(reader: Any | None) -> tuple[Any, Any, _ClaimsClient]:
    """One invoke composition whose composite dispatch reaches the real handlers."""
    client = _ClaimsClient()

    async def caller_client(_tenant: str) -> _ClaimsClient:
        return client

    async def verify_current(current: Any) -> Any:
        return current

    async def service_client(_tenant: str) -> _ClaimsClient:
        return client

    async def service_claims(_tenant: str, required_scopes: frozenset[str]) -> dict:
        return {
            "principal": "service:arbora",
            "tenant": _tenant,
            "scopes": sorted(required_scopes),
            "policy_version": "v1",
            "delegation": False,
        }

    async def check_access(_client: Any, _current: Any, _subject: str) -> bool:
        return False

    async def eg_dispatch(_binding: Any, _params: Any, _context: Any) -> Any:
        raise AssertionError("source reads never dispatch the engine port")

    runtime = BoundOperationRuntime(
        caller_client=caller_client,
        verify_current=verify_current,
        service_client=service_client,
        service_claims=service_claims,
        check_access=check_access,
        eg_dispatch=eg_dispatch,
        service_scopes=frozenset({"example:execute"}),
        service_principal="service:arbora",
        bindings={} if reader is None else {"source_reader": reader},
    )
    services, *_rest = setup_services(
        effects=None,
        registry=Registry(source_ops.operations()),
        runtime=runtime,
    )
    return services, runtime, client


@pytest.mark.parametrize("op_id", sorted(EXPECTED_SCOPES))
@pytest.mark.parametrize("surface", [Surface.HTTP, Surface.MCP])
def test_source_reads_served(op_id: str, surface: Surface):
    reader = FakeReader()
    services, _runtime, client = _services(reader)
    # Per-principal admission: the caller holds exactly the op's engine scopes.
    supplied = caller(effective_scopes=EXPECTED_SCOPES[op_id])
    result = asyncio.run(
        invoke(op_id, dict(PARAMS[op_id]), supplied, surface, services=services)
    )
    assert isinstance(result, OpResult) and result.code == "OK", result
    served = result.value
    assert served["value"]["tenant"] == "tenant:one"
    if "found" in served["value"]:
        assert served["value"]["found"] is True
    else:
        assert served["value"]["entries"], "the history must list its refs"
    assert reader.calls == [(op_id.split(".")[-1], dict(PARAMS[op_id]))]
    bound = client.claims
    assert bound is None  # claims are released after the call
    seen = reader.seen_claims[0]
    assert seen["principal"] == "person:one"
    assert seen["tenant"] == "tenant:one"
    assert set(seen["scopes"]) == EXPECTED_SCOPES[op_id]
    # The outcome is the reader's own model, nested for the result envelope.
    assert served["value"]["repository"] == "repo-a"
    if op_id == "source.blob":
        assert (
            served["value"]["content_sha256"]
            == hashlib.sha256(b"print('hello')\n").hexdigest()
        )
        assert served["value"]["content_encoding"] == "utf-8"


@pytest.mark.parametrize("surface", [Surface.HTTP, Surface.MCP])
def test_source_blob_binary_round_trips(surface: Surface):
    reader = FakeReader()
    reader.blob_content = b"\xff\x00"
    services, _runtime, _client = _services(reader)
    result = asyncio.run(
        invoke(
            "source.blob",
            dict(PARAMS["source.blob"]),
            caller(effective_scopes=FULL_SCOPES),
            surface,
            services=services,
        )
    )
    assert isinstance(result, OpResult) and result.code == "OK", result
    value = result.value["value"]
    assert value["content"] == "/wA=" and value["content_encoding"] == "base64"
    assert value["content_sha256"] == hashlib.sha256(reader.blob_content).hexdigest()
    assert (
        source_ops.SourceBlobOutcome.model_validate(value).content
        == reader.blob_content
    )


@pytest.mark.parametrize(
    ("encoding", "content"),
    [("base64", "%%%"), ("utf-8", b"\xff"), ("unknown", "data"), ([], "data")],
)
def test_source_blob_rejects_invalid_wire_content(
    encoding: object, content: str | bytes
):
    with pytest.raises(ValueError):
        source_ops.SourceBlobOutcome.model_validate(
            {
                "tenant": "t",
                "repository": "r",
                "ref": "main",
                "path": "p",
                "found": True,
                "content": content,
                "content_encoding": encoding,
            }
        )


def test_source_blob_encodes_binary_bytearray():
    outcome = source_ops.SourceBlobOutcome.model_validate(
        {
            "tenant": "t",
            "repository": "r",
            "ref": "main",
            "path": "p",
            "found": True,
            "content": bytearray(b"\xff"),
        }
    )
    value = outcome.model_dump(mode="json")
    assert value["content"] == "/w==" and value["content_encoding"] == "base64"


@pytest.mark.parametrize("op_id", sorted(EXPECTED_SCOPES))
def test_uncomposed_reader_refuses_unavailable(op_id: str):
    services, _runtime, _client = _services(None)
    result = asyncio.run(
        invoke(
            op_id,
            dict(PARAMS[op_id]),
            caller(effective_scopes=FULL_SCOPES),
            Surface.HTTP,
            services=services,
        )
    )
    assert result.code == "UNAVAILABLE"
    assert isinstance(result, OpError), result
    assert result.details == {"reason": "source reader is not composed"}
    assert result.source == "arbora"


@pytest.mark.parametrize(
    "op_id,missing",
    [
        ("source.blob", "blob:read"),
        ("source.tree", "edge:read"),
        ("source.history", "node:read"),
        ("source.blame", "edge:read"),
    ],
)
def test_missing_scope_refuses_before_reader(op_id: str, missing: str):
    reader = FakeReader()
    services, _runtime, client = _services(reader)
    supplied = caller(effective_scopes=EXPECTED_SCOPES[op_id] - {missing})
    result = asyncio.run(
        invoke(op_id, dict(PARAMS[op_id]), supplied, Surface.HTTP, services=services)
    )
    assert result.code == "SCOPE_REQUIRED"
    assert result.details == {"missing_scopes": [missing]}
    assert reader.calls == [] and client.claims is None


@pytest.mark.parametrize(
    "params",
    [
        {"repository": "repo-a", "ref": "main", "path": "/absolute"},
        {"repository": "repo-a", "ref": "main", "path": "a/../b"},
        {"repository": "repo-a", "ref": "main", "path": "a//b"},
        {"repository": "repo-a", "ref": "main", "path": "trailing/"},
        {"repository": "repo-a", "ref": "main", "path": " padded"},
        {"repository": "", "ref": "main", "path": "src"},
        {"repository": "repo-a", "ref": "", "path": "src"},
        {"repository": "r" * 1025, "ref": "main", "path": "src"},
        {"repository": "repo-a", "ref": "main", "path": "src", "tenant": "other"},
    ],
)
def test_invalid_params_refuse_before_authority(params: dict[str, Any]):
    reader = FakeReader()
    services, _runtime, client = _services(reader)
    result = asyncio.run(
        invoke(
            "source.tree",
            params,
            caller(effective_scopes=FULL_SCOPES),
            Surface.HTTP,
            services=services,
        )
    )
    assert result.code == "INVALID_ARGUMENT"
    assert reader.calls == [] and client.claims is None


def test_operation_specs_are_read_only_composites_with_exact_engine_scopes():
    ops = {op.id: op for op in source_ops.operations()}
    assert set(ops) == set(EXPECTED_SCOPES)
    for op_id, op in ops.items():
        assert op.verb == Verb.FIND
        assert op.effect == Effect.READ
        assert op.idempotency == Idempotency.NONE
        assert op.scopes == EXPECTED_SCOPES[op_id]
        assert isinstance(op.binding, Composite)
        assert (
            op.binding.handler
            == f"arbora.api.ops.source.handle_source_{op_id.split('.')[1]}"
        )


def test_native_reader_satisfies_the_typed_port():
    from arbora.mcp_server.source_reader import NativeSourceReader

    assert isinstance(NativeSourceReader(), source_ops.SourceReader)
