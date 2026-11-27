"""Knowledge bundle operations and transfer routes over the served path (ARBORA-OPS-R923).

The app is the real served composition: every request authenticates and drives a bundle
operation through the invoke chokepoint to a recording engine that holds blobs per tenant.
The bundle methods are engine ``main`` shapes, so the double declares them.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from test_invoke_contract import FixtureLeases

from arbora.access.action_policy import ActionPolicies
from arbora.api.ops.bundle import (
    MAX_BUNDLE_BYTES,
    BundleExportParams,
    BundleImportParams,
    BundleImportResult,
)
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.security import request_identity
from arbora.security.key_cache import JwksCache
from tests.api.test_sparql_protocol import _EffectItems
from tests.test_served_operation_api import FakeAudit, FakeClient, FakePool, _actor

READ = ("graph:read",)
WRITE = ("graph:read", "graph:write")
#: The bundle is two chunks so the transfer is a stream, not one read.
BUNDLE = b"a" * 1_048_576 + b"tail"


class _Store:
    """The engine blob store and bundle table, shared by every tenant's client."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.staging: dict[int, bytearray] = {}
        self.imports: list[tuple[str, dict[str, Any]]] = []


class _Audit(FakeAudit):
    """The audit ledger answers the graph of the tenant it records for."""

    def __init__(self, tenant: str) -> None:
        super().__init__()
        self.tenant = tenant

    async def audit_append(self, **fields: Any) -> dict[str, Any]:
        receipt = await super().audit_append(**fields)
        return {**receipt, "graph": self.tenant}


class _Engine(FakeClient):
    def __init__(self, store: _Store, tenant: str) -> None:
        super().__init__()
        self.admin = _Audit(tenant)
        self.control_leases = FixtureLeases()
        self.store, self.tenant = store, tenant
        self.journal = _EffectItems()
        self.work_items = self.journal  # type: ignore[assignment]

    def methods(self) -> list[str]:
        return [method for method, _p, _g in self.sent]

    async def _send(
        self, method: str, params: Any, graph: str | None, **kw: Any
    ) -> Any:
        handler = getattr(self, f"_m_{method}", None)
        if handler is None:
            return await super()._send(method, params, graph, **kw)
        self.sent.append((method, params, graph))
        return handler(params)

    def _m_BundleExport(self, p: dict[str, Any]) -> Any:
        digest = hashlib.sha256(BUNDLE).hexdigest()
        self.store.blobs[(self.tenant, digest)] = BUNDLE
        return {
            "bundle_ref": digest,
            "manifest_digest": digest,
            "size_bytes": len(BUNDLE),
        }

    def _m_BundleImport(self, p: dict[str, Any]) -> Any:
        self.store.imports.append((self.tenant, p))
        return {
            "documents": 2,
            "embeddings": 0,
            "graphs": [],
            "provenance": 0,
            "triples": 7,
        }

    def _m_BlobFetchBegin(self, p: dict[str, Any]) -> Any:
        from medulla import EngineResponseError

        if (self.tenant, p["digest"]) not in self.store.blobs:
            raise EngineResponseError("BLOB_NOT_FOUND", "no such blob")
        return [1, 2]

    def _m_BlobChunkGet(self, p: dict[str, Any]) -> Any:
        blob = self.store.blobs[(self.tenant, hashlib.sha256(BUNDLE).hexdigest())]
        return blob[p["idx"] * 1_048_576 : (p["idx"] + 1) * 1_048_576]

    def _m_BlobFetchEnd(self, p: dict[str, Any]) -> Any:
        return None

    def _m_BlobBegin(self, p: dict[str, Any]) -> Any:
        self.store.staging[len(self.store.staging) + 1] = bytearray()
        return len(self.store.staging)

    def _m_BlobChunkPut(self, p: dict[str, Any]) -> Any:
        self.store.staging[p["cursor"]] += bytes(p["data"])
        return None

    def _m_BlobCommit(self, p: dict[str, Any]) -> Any:
        data = bytes(self.store.staging[p["cursor"]])
        digest = hashlib.sha256(data).hexdigest()
        self.store.blobs[(self.tenant, digest)] = data
        return {"digest": digest, "size": len(data)}


#: Each tenant's verified actor by its synthetic bearer credential.
_ACTORS: dict[str, Any] = {}


def _declare_bundle_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated
    from medulla.generated import _runtime

    declared = frozenset(generated.METHOD_IDS) | {"BundleExport", "BundleImport"}
    monkeypatch.setattr(generated, "METHOD_IDS", declared)
    monkeypatch.setattr(_runtime, "METHOD_IDS", declared)


def _app(
    monkeypatch: pytest.MonkeyPatch,
    store: _Store,
    roles: tuple[str, ...],
    tenant: str = "tenant:one",
) -> tuple[FastAPI, _Engine]:
    _declare_bundle_methods(monkeypatch)
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "arbora-fixture")
    monkeypatch.setenv("KG_POLICY_VERSION", "policy:fixture")
    monkeypatch.setenv("AUTH_JWT_JWKS_URI", "https://identity.invalid/jwks")
    base = _actor()
    actor = replace(base, roles=roles, tenant_id=tenant)

    _ACTORS[f"fixture-verified-{tenant}"] = actor

    async def credential_owner(credential: str, **_kw) -> Any:
        return _ACTORS[credential]

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    engine = _Engine(store, tenant)
    process = request_identity.mint_graph_session(actor)
    invocation = compose_served_invocation(
        config=RuntimeConfig(),
        keys=JwksCache(),
        pool=FakePool(engine),
        graph=process.graph,
        process_session=lambda: process,
        action_policies=ActionPolicies(),
        bindings={},
        fleet_effect=refused_fleet_effect,
    )
    app = FastAPI()
    invocation.mount_http(app, prefix="/api")
    app.state.token = f"fixture-verified-{tenant}"
    return app, engine


def _call(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    token = app.state.token
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}

    async def go() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as http:
            return await http.request(method, path, headers=headers, **kwargs)

    return asyncio.run(go())


def _op(app: Any, op: str, params: dict[str, Any], **kwargs: Any) -> httpx.Response:
    headers = {"Idempotency-Key": f"key-{op}", **kwargs.pop("headers", {})}
    return _call(
        app, "POST", f"/api/v1/ops/{op}", json=params, headers=headers, **kwargs
    )


@pytest.mark.spec("ARBORA-OPS-R923.1")
def test_params_round_trip_and_reject_unknown_fields() -> None:
    export = BundleExportParams(graphs=["g1", "g2"], include_documents=True)
    assert BundleExportParams.model_validate(export.model_dump()) == export
    imported = BundleImportParams(bundle_ref="ref-1")
    assert imported.on_conflict == "refuse" and imported.target_graph_prefix == ""
    with pytest.raises(ValidationError):
        BundleExportParams(graphs=[f"g{i}" for i in range(33)])
    with pytest.raises(ValidationError):
        BundleExportParams(graphs=["g"], surprise=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BundleImportParams(bundle_ref="r", on_conflict="overwrite")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        BundleImportResult(receipt_ref="A" * 64, triples=1, documents=1)
    ok = BundleImportResult(receipt_ref="a" * 64, triples=1, documents=1)
    assert ok.receipt_ref == "a" * 64


@pytest.mark.spec("ARBORA-OPS-R923.2")
def test_routes_stream_chunks_and_import_needs_an_approved_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    app, engine = _app(monkeypatch, store, WRITE)
    exported = _op(app, "bundle.export", {"graphs": ["g1"]})
    assert exported.status_code == 200, exported.text
    ref = hashlib.sha256(BUNDLE).hexdigest()
    streamed = _call(app, "GET", f"/api/v1/bundles/{ref}")
    assert streamed.status_code == 200 and streamed.content == BUNDLE
    uploaded = _call(app, "PUT", "/api/v1/bundles/new", content=BUNDLE)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["bundle_ref"] == ref
    assert engine.methods().count("BlobChunkPut") == 2
    # An unapproved import returns the plan preview and writes nothing.
    preview = _op(app, "bundle.import", {"bundle_ref": ref})
    assert preview.status_code != 200, preview.text
    assert "BundleImport" not in engine.methods()
    assert store.imports == []


@pytest.mark.spec("ARBORA-OPS-R923.3")
def test_refusals_and_foreign_ref_is_indistinguishable_from_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    owner, _ = _app(monkeypatch, store, WRITE)
    assert _op(owner, "bundle.export", {"graphs": ["g1"]}).status_code == 200
    ref = hashlib.sha256(BUNDLE).hexdigest()
    other, other_engine = _app(monkeypatch, store, READ, tenant="tenant:two")
    foreign = _call(other, "GET", f"/api/v1/bundles/{ref}")
    absent = _call(other, "GET", f"/api/v1/bundles/{'0' * 64}")
    assert foreign.status_code == absent.status_code == 404
    assert _strip(foreign.json()) == _strip(absent.json())
    # Without the scope the call is refused before any engine call.
    none, none_engine = _app(monkeypatch, store, ("kg:read",), tenant="tenant:two")
    denied = _call(none, "PUT", "/api/v1/bundles/new", content=b"x")
    assert denied.status_code == 403
    assert none_engine.methods() == []
    # A body over 4 GiB is refused with BUNDLE_TOO_LARGE before any stage opens.
    big, big_engine = _app(monkeypatch, store, WRITE)
    refused = _call(
        big,
        "PUT",
        "/api/v1/bundles/new",
        content=b"x",
        headers={"Content-Length": str(MAX_BUNDLE_BYTES + 1)},
    )
    assert refused.status_code == 413
    assert "BUNDLE_TOO_LARGE" in refused.text
    assert "BlobBegin" not in big_engine.methods()
    assert other_engine.methods().count("BlobChunkGet") == 0


def _strip(body: dict[str, Any]) -> dict[str, Any]:
    """The envelope without its per-request identifiers."""
    kept = {k: v for k, v in body.items() if k != "error"}
    error = {k: v for k, v in body["error"].items() if k not in {"request_id", "ref"}}
    return {**kept, "error": error}


@pytest.mark.spec("ARBORA-OPS-R923")
@pytest.mark.spec("ARBORA-OPS-R923.4")
def test_export_download_upload_import_between_tenants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    one, _one = _app(monkeypatch, store, WRITE, tenant="tenant:one")
    two, two_engine = _app(monkeypatch, store, WRITE, tenant="tenant:two")
    exported = _op(one, "bundle.export", {"graphs": ["g1"]}).json()
    ref = _find(exported, "bundle_ref")
    downloaded = _call(one, "GET", f"/api/v1/bundles/{ref}").content
    uploaded = _call(two, "PUT", "/api/v1/bundles/in", content=downloaded)
    assert uploaded.status_code == 200
    staged = uploaded.json()["bundle_ref"]
    assert staged == ref
    preview = _op(two, "bundle.import", {"bundle_ref": staged})
    plan_ref = _find(preview.json(), "plan_ref")
    approved = _op(
        two,
        "bundle.import",
        {"bundle_ref": staged},
        headers={"Arbora-Plan-Ref": plan_ref},
    )
    assert approved.status_code == 200, approved.text
    assert _find(approved.json(), "triples") == 7
    assert [t for t, _p in store.imports] == ["tenant:two"]
    assert two_engine.methods().count("BundleImport") == 1


def _children(body: Any) -> list[Any]:
    """The nested values of a dict or list."""
    return list(body.values()) if isinstance(body, dict) else list(body)


def _find(body: Any, name: str) -> Any:
    """The first value named ``name`` anywhere in a response envelope."""
    if isinstance(body, dict) and name in body:
        return body[name]
    nested = _children(body) if isinstance(body, (dict, list)) else []
    found = (_find(value, name) for value in nested)
    return next((value for value in found if value is not None), None)
