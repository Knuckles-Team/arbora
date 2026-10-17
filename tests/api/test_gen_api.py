"""Focused contract generation checks; no repository-wide scan required."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from graph_os.client._schema import _resolve_local_refs

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/gen_api.py"
SPEC = importlib.util.spec_from_file_location("gen_api", SCRIPT)
assert SPEC and SPEC.loader
gen_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen_api)


def _registry() -> dict:
    return {
        "api_version": "1",
        "registry_digest": "a" * 64,
        "ops": [
            {
                "id": "query.uql",
                "verb": "ask",
                "summary": "Run UQL",
                "examples": ["ask the graph"],
                "params": {"schema": {"type": "object"}},
                "result": {"schema": {"type": "object"}},
                "http": None,
            }
        ],
    }


@pytest.fixture
def engine_contract(tmp_path, monkeypatch):
    from graph_os.api.registry import eg_binding

    contract = tmp_path / "contract"
    contract.mkdir()
    (contract / "errors.json").write_text(
        json.dumps(
            {
                "contract_version": 1,
                "errors": [
                    {"code": "SYNTHETIC", "http_status_hint": 409, "retryable": False}
                ],
            }
        )
    )
    provider = ModuleType("epistemic_graph.contract")
    provider.RECEIPT_DIGEST = "b" * 64
    calls = []
    provider.verify_receipt = calls.append
    exceptions = ModuleType("epistemic_graph.contract_errors")
    exceptions.ContractDigestMismatch = type("SyntheticMismatch", (RuntimeError,), {})
    monkeypatch.setitem(sys.modules, provider.__name__, provider)
    monkeypatch.setitem(sys.modules, exceptions.__name__, exceptions)
    monkeypatch.setattr(eg_binding.resources, "files", lambda package: tmp_path)
    return provider, contract, calls


def _http_ref(document, value):
    return gen_api._pointer(document, value["$ref"][1:]) if "$ref" in value else value


def test_generator_emits_deterministic_artifacts(engine_contract) -> None:
    first = gen_api.generate(_registry())
    assert first == gen_api.generate(_registry())
    openapi = json.loads(first[gen_api.ROOT / "docs/api/openapi.json"])
    assert (
        openapi["paths"]["/api/v1/ops/query.uql"]["post"]["operationId"] == "query.uql"
    )
    assert (
        json.loads(first[gen_api.GENERATED / "registry.json"])["registry_digest"]
        == "a" * 64
    )
    models = first[gen_api.ROOT / "graph_os/client/_generated_models.py"]
    assert len(models.splitlines()) < 900
    compile(models, "_generated_models.py", "exec")
    assert gen_api.ROOT / "graph_os/client/invoke.py" not in first


def test_generator_fails_closed_on_an_empty_registry() -> None:
    with pytest.raises(ValueError, match="empty API registry"):
        gen_api.generate({"ops": []})


def test_compat_detects_stable_breaks_but_allows_additive_changes() -> None:
    before = _registry()
    before["ops"][0]["stability"] = "stable"
    before["ops"][0]["params"]["schema"] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }
    before["ops"][0]["result"]["schema"] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
    }
    after = json.loads(json.dumps(before))
    after["ops"][0]["params"]["schema"]["properties"]["limit"] = {"type": "integer"}
    assert gen_api._breaking_changes(before, after) == []
    after["ops"][0]["params"]["schema"]["required"].append("limit")
    after["ops"][0]["result"]["schema"]["properties"].pop("answer")
    changes = gen_api._breaking_changes(before, after)
    assert any("new required params" in row for row in changes)
    assert any("removed result fields" in row for row in changes)


def test_openapi_references_shared_provider_document(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "method.request.json").write_text(
        json.dumps(
            {
                "$defs": {"Payload": {"type": "object"}},
                "methods": {"Create": {"$ref": "#/$defs/Payload"}},
            }
        )
    )
    registry = _registry()
    registry["ops"][0]["params"] = {
        "eg_schema": "contract/schemas/method.request.json#/methods/Create"
    }
    result = gen_api._openapi(registry, tmp_path)
    body = result["paths"]["/api/v1/ops/query.uql"]["post"]["requestBody"]
    request = _http_ref(result, body)
    assert request["content"]["application/json"]["schema"] == {
        "$ref": "schemas/method.request.json#/methods/Create"
    }
    assert {"type": "object"} in result["components"]["schemas"].values()


def _eg_compat_registry(
    root: Path, *, required_limit: bool, answer: bool, references: bool = False
) -> dict:
    root.mkdir(parents=True)
    request = {
        "$defs": {
            "Params": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["text", "limit"] if required_limit else ["text"],
            }
        },
        "methods": {"Uql": {"properties": {"params": {"$ref": "#/$defs/Params"}}}},
    }
    response = {
        "$defs": {
            "Result": {
                "type": "object",
                "properties": {"answer": {"type": "string"}} if answer else {},
            }
        },
        "methods": {
            "Uql": {"bodies": {"result": {"schema": {"$ref": "#/$defs/Result"}}}}
        },
    }
    registry = _registry()
    registry["ops"][0]["stability"] = "stable"
    for field, name, document in (
        ("params", "method.request.json", request),
        ("result", "result.query.json", response),
    ):
        path = root / name
        path.write_text(json.dumps(document))
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        registry["ops"][0][field] = {
            "eg_schema": f"contract/schemas/{name}#/methods/Uql",
            "schema_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    return _fixture_snapshots(registry, root, references)


def _fixture_snapshots(registry: dict, root: Path, references: bool) -> dict:
    gen_api._snapshot_eg_schemas(registry, root.parent)
    return (
        registry
        if references
        else gen_api._expand_registry(
            registry, lambda name: (root.parent / name).read_bytes()
        )
    )


def test_compat_resolves_eg_refs_and_detects_nested_breaks(tmp_path: Path) -> None:
    baseline = _eg_compat_registry(
        tmp_path / "before" / "schemas", required_limit=False, answer=True
    )
    candidate = _eg_compat_registry(
        tmp_path / "after" / "schemas", required_limit=True, answer=False
    )
    changes = gen_api._breaking_changes(baseline, candidate)
    assert any("new required params params.params" in change for change in changes)
    assert any("removed result fields" in change for change in changes)


def test_compat_fails_closed_when_eg_schema_hash_is_stale(tmp_path: Path) -> None:
    registry = _eg_compat_registry(
        tmp_path / "contract" / "schemas", required_limit=False, answer=True
    )
    registry["ops"][0]["params"]["schema_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="EG schema digest changed"):
        gen_api._snapshot_eg_schemas(registry, tmp_path / "contract")


def test_compat_requires_review_for_unclassified_eg_schema_change(
    tmp_path: Path,
) -> None:
    baseline = _eg_compat_registry(
        tmp_path / "contract" / "schemas", required_limit=False, answer=True
    )
    candidate = json.loads(json.dumps(baseline))
    candidate["ops"][0]["params"]["schema_sha256"] = "1" * 64
    assert "review required" in " ".join(gen_api._breaking_changes(baseline, candidate))


def test_engine_emitter_binds_exact_provider_and_registry(engine_contract):
    provider, _, calls = engine_contract
    output = gen_api.generate(_registry())[gen_api.GENERATED / "engine_errors.py"]
    namespace = {}
    exec(compile(output, "engine_errors.py", "exec"), namespace)
    assert namespace["REGISTRY_DIGEST"] == _registry()["registry_digest"]
    assert namespace["EG_RECEIPT_DIGEST"] == provider.RECEIPT_DIGEST
    assert namespace["ENGINE_ERRORS"] == {"SYNTHETIC": (409, False)}
    assert calls == [provider.RECEIPT_DIGEST, provider.RECEIPT_DIGEST]


def test_engine_emitter_refuses_absent_error_contract(engine_contract):
    _, contract, _ = engine_contract
    (contract / "errors.json").unlink()
    with pytest.raises(ValueError):
        gen_api.generate(_registry())


@pytest.fixture
def emitted_client(engine_contract, tmp_path, monkeypatch):
    """Execute emitted modules with the registry bytes emitted beside them."""
    artifacts = gen_api.generate(_registry())
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(artifacts[gen_api.GENERATED / "registry.json"])
    package = ModuleType("emitted_client")
    package.__path__ = [str(tmp_path)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    for name in ("_generated_models", "invoke"):
        module = ModuleType(f"{package.__name__}.{name}")
        monkeypatch.setitem(sys.modules, module.__name__, module)
        path = gen_api.ROOT / f"graph_os/client/{name}.py"
        source = artifacts[path] if path in artifacts else path.read_bytes()
        exec(compile(source, f"{name}.py", "exec"), module.__dict__)
    models = sys.modules["emitted_client._generated_models"]
    monkeypatch.setattr(models.importlib.resources, "files", lambda _name: tmp_path)
    return sys.modules["emitted_client.invoke"]


@pytest.mark.spec("GRAPHOS-OPS-R025.1")
@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
async def test_emitted_invoker_preserves_matching_envelope(emitted_client, ok):
    envelope = {
        "ok": ok,
        "result": {"rows": [1]},
        "error": {"code": "STEP_UP_REQUIRED", "details": {"plan_ref": "opaque"}},
        "meta": {"registry_digest": _registry()["registry_digest"]},
    }
    calls = []

    class Transport:
        async def invoke(self, op_id, params):
            calls.append((op_id, params))
            return envelope

    params = {"query": "MATCH ()"}
    assert await emitted_client.invoke(Transport(), "query.uql", params) is envelope
    assert calls == [("query.uql", params)]


@pytest.mark.spec("GRAPHOS-OPS-R025.1")
@pytest.mark.asyncio
@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("meta", [None, {}, [], {"registry_digest": "stale"}])
async def test_emitted_invoker_refuses_missing_or_stale_digest(
    emitted_client, ok, meta
):
    class Transport:
        async def invoke(self, op_id, params):
            return {"ok": ok, "meta": meta}

    with pytest.raises(RuntimeError, match="GraphOS registry digest mismatch"):
        await emitted_client.invoke(Transport(), "query.uql", {})


@pytest.mark.spec("GRAPHOS-OPS-R025.1")
@pytest.mark.asyncio
async def test_emitted_invoker_rejects_unknown_operation_before_transport(
    emitted_client,
):
    calls = []

    class Transport:
        async def invoke(self, op_id, params):
            calls.append(op_id)
            return {}

    with pytest.raises(KeyError, match="unknown.operation"):
        await emitted_client.invoke(Transport(), "unknown.operation", {})
    assert calls == []


@pytest.mark.spec("GRAPHOS-OPS-R025.2")
def test_reference_resources_preserve_emitted_lookup_and_digest(
    engine_contract, monkeypatch, tmp_path
):
    _, contract, _ = engine_contract
    registry = _eg_compat_registry(
        contract / "schemas", required_limit=False, answer=True, references=True
    )
    expected = gen_api._expand_registry(
        registry, lambda name: (contract / name).read_bytes()
    )
    artifacts = gen_api.generate(registry)
    assert artifacts == gen_api.generate(registry)
    destination = tmp_path / "installed"
    destination.mkdir()
    for path, content in artifacts.items():
        if path.is_relative_to(gen_api.GENERATED):
            target = destination / path.relative_to(gen_api.GENERATED)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    for filename in ("method.request.json", "result.query.json"):
        assert (
            artifacts[gen_api.GENERATED / "schemas" / filename]
            == artifacts[gen_api.ROOT / "docs/api/schemas" / filename]
        )
    module = ModuleType("reference_client")
    exec(
        compile(
            artifacts[gen_api.ROOT / "graph_os/client/_generated_models.py"],
            "reference_client.py",
            "exec",
        ),
        module.__dict__,
    )
    monkeypatch.setattr(module.importlib.resources, "files", lambda name: destination)
    actual = module.schema_for("query.uql")
    assert actual == {
        field: expected["ops"][0][field] for field in ("params", "result")
    }
    assert module.REGISTRY_DIGEST == registry["registry_digest"]
    (destination / "schemas/method.request.json").unlink()
    with pytest.raises(FileNotFoundError):
        module.schema_for("query.uql")


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.json#/x",
        "schemas/../../outside.json#/x",
        "/schemas/a.json#/x",
        "https://host/schema.json#/x",
        "schemas/a.json",
        "schemas/a.json#bad",
        "schemas/a.json?x#/x",
        "schemas/a%2f..json#/x",
    ],
)
def test_schema_resource_resolver_rejects_unsafe_reference_before_read(reference):
    def forbidden_read(name):
        raise AssertionError("unsafe reference must not be read")

    spec = {
        "eg_schema": "contract/" + reference,
        "schema_snapshot": {"$ref": reference},
    }
    with pytest.raises(ValueError):
        gen_api._expanded_spec(spec, forbidden_read)


def test_schema_resource_resolver_rejects_digest_pointer_and_external_refs(tmp_path):
    registry = _eg_compat_registry(
        tmp_path / "schemas", required_limit=False, answer=True, references=True
    )
    spec = registry["ops"][0]["params"]

    def read(name):
        return (tmp_path / name).read_bytes()

    with pytest.raises(ValueError, match="digest mismatch"):
        gen_api._expanded_spec({**spec, "schema_sha256": "0" * 64}, read)
    missing = "schemas/method.request.json#/absent"
    with pytest.raises(ValueError, match="missing EG schema pointer"):
        gen_api._expanded_spec(
            {
                **spec,
                "eg_schema": "contract/" + missing,
                "schema_snapshot": {"$ref": missing},
            },
            read,
        )
    with pytest.raises(ValueError, match="unsafe schema reference"):
        _resolve_local_refs({"$ref": "https://external/schema"}, {}, frozenset())


def test_openapi_namespaces_curated_local_definitions():
    registry = _registry()
    registry["ops"][0]["params"]["schema"] = {
        "$defs": {"Item": {"type": "string"}},
        "properties": {"item": {"$ref": "#/$defs/Item"}},
    }
    document = gen_api._openapi(registry)
    body = document["paths"]["/api/v1/ops/query.uql"]["post"]["requestBody"]
    request = _http_ref(document, body)
    model = _resolve_local_refs(
        request["content"]["application/json"]["schema"], document, frozenset()
    )
    assert model["properties"]["item"] == {"type": "string"}


def test_compat_reads_baseline_schema_resources_from_same_git_ref(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    before = _eg_compat_registry(
        before_root / "schemas", required_limit=False, answer=True, references=True
    )
    after = _eg_compat_registry(
        after_root / "contract/schemas",
        required_limit=True,
        answer=True,
        references=True,
    )
    reads = []

    def git_call(argv, **kwargs):
        if argv[1] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout=b"baseline")
        path = argv[2]
        reads.append(path)
        prefix = "baseline:graph_os/api/generated/"
        assert path.startswith(prefix)
        relative = path.removeprefix(prefix)
        payload = (
            json.dumps(before).encode()
            if relative == "registry.json"
            else (before_root / relative).read_bytes()
        )
        return SimpleNamespace(returncode=0, stdout=payload)

    monkeypatch.setattr(gen_api.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(gen_api.subprocess, "run", git_call)
    monkeypatch.setattr(gen_api.importlib.resources, "files", lambda name: after_root)
    assert gen_api._check_compat(after, "baseline") == 1
    assert "baseline:graph_os/api/generated/schemas/method.request.json" in reads


@pytest.mark.spec("GRAPHOS-OPS-R025.2")
def test_openapi_reuses_curated_schemas_without_changing_resolved_meaning():
    registry = _registry()
    schema = {
        "type": "object",
        "properties": {"first": {"type": "string"}, "second": {"type": "string"}},
        "examples": [
            {
                "properties": {"first": "literal data"},
                "$ref": "#literal",
                "$id": "literal",
            }
        ],
    }
    registry["ops"][0]["params"]["schema"] = schema
    registry["ops"][0]["result"]["schema"] = schema
    document = gen_api._openapi(registry)
    operation = document["paths"]["/api/v1/ops/query.uql"]["post"]
    request_body = _http_ref(document, operation["requestBody"])
    request = request_body["content"]["application/json"]["schema"]
    response_body = _http_ref(document, operation["responses"]["200"])
    response = response_body["content"]["application/json"]["schema"]
    assert request == response
    component = gen_api._pointer(document, request["$ref"][1:])
    assert component["examples"] == schema["examples"]
    validation = {key: value for key, value in component.items() if key != "examples"}
    expected = {key: value for key, value in schema.items() if key != "examples"}
    assert _resolve_local_refs(validation, document, frozenset()) == expected
    assert component["properties"]["first"] == component["properties"]["second"]


@pytest.mark.parametrize("keyword", ["$id", "$anchor", "$dynamicRef", "$dynamicAnchor"])
def test_openapi_refuses_scope_changing_schema_keywords(keyword):
    registry = _registry()
    registry["ops"][0]["params"]["schema"] = {"type": "string", keyword: "scope"}
    with pytest.raises(ValueError, match="cannot be hoisted"):
        gen_api._openapi(registry)


def test_openapi_keeps_recursive_definition_references_closed():
    registry = _registry()
    registry["ops"][0]["params"]["schema"] = {
        "$defs": {
            "Node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Node"}}}
        },
        "$ref": "#/$defs/Node",
    }
    document = gen_api._openapi(registry)

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert gen_api._pointer(document, value["$ref"][1:]) is not None
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)


@pytest.mark.parametrize(
    "position", ["unevaluatedItems", "contentSchema", "additionalItems"]
)
def test_openapi_rewrites_nested_schema_positions_and_refuses_scoped_ids(position):
    registry = _registry()
    schema = {"$defs": {"Item": {"type": "string"}}, position: {"$ref": "#/$defs/Item"}}
    registry["ops"][0]["params"]["schema"] = schema
    document = gen_api._openapi(registry)
    operation = document["paths"]["/api/v1/ops/query.uql"]["post"]
    body = _http_ref(document, operation["requestBody"])
    resolved = _resolve_local_refs(
        body["content"]["application/json"]["schema"], document, frozenset()
    )
    assert resolved[position] == {"type": "string"}
    schema[position] = {"$id": "relative-scope", "type": "string"}
    with pytest.raises(ValueError, match="cannot be hoisted"):
        gen_api._openapi(registry)
