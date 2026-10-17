#!/usr/bin/env python3
"""Generate the versioned GraphOS API contract from its declared registry.

A missing or empty registry fails closed; a hand-maintained contract is not
an acceptable substitute for a generated one.

Engine errors and their provider/registry identity are emitted together from the
installed pinned provider. Missing package evidence stops generation.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.resources
import json
import shutil
import subprocess  # nosec B404 -- fixed-argv git compatibility check
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "graph_os/api/generated"
sys.path.insert(0, str(ROOT))

from graph_os.client._schema import (
    _expanded_spec,
    _pointer,
    _resolve_local_refs,
    _resource_parts,
)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode()


def _registry(module_name: str) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    source = getattr(module, "get_registry", None)
    if source is None:
        source = getattr(module, "REGISTRY", None)
    if source is None:
        raise ValueError(f"{module_name} has no get_registry or REGISTRY")
    registry = source() if callable(source) else source
    document = json.loads(registry.canonical)
    if not document.get("ops"):
        raise ValueError("refusing to generate an empty API registry")
    document["registry_digest"] = registry.digest
    _snapshot_eg_schemas(document)
    return document


def _snapshot_eg_schemas(
    registry: dict[str, Any], contract_root: Path | None = None
) -> None:
    """Record digest-bound references to shared provider schema documents."""
    root = contract_root or Path(
        str(importlib.resources.files("epistemic_graph") / "contract")
    )
    documents: dict[Path, dict] = {}
    for op in registry["ops"]:
        for field in ("params", "result"):
            spec = op[field]
            reference = spec.get("eg_schema")
            if reference is None:
                continue
            if not reference.startswith("contract/"):
                raise ValueError(f"invalid EG schema reference: {reference!r}")
            filename, pointer = _resource_parts(reference.removeprefix("contract/"))
            relative = Path(filename)
            if relative not in documents:
                documents[relative] = json.loads((root / relative).read_bytes())
            document = documents[relative]
            canonical = json.dumps(
                document, sort_keys=True, separators=(",", ":")
            ).encode()
            if hashlib.sha256(canonical).hexdigest() != spec.get("schema_sha256"):
                raise ValueError(
                    f"EG schema digest changed during generation: {reference}"
                )
            _pointer(document, pointer)
            spec["schema_snapshot"] = {"$ref": f"{relative.as_posix()}#{pointer}"}


def _descriptors(registry: dict[str, Any]) -> dict[str, Any]:
    return {
        "api_version": registry["api_version"],
        "registry_digest": registry["registry_digest"],
        "ops": [
            {
                "id": op["id"],
                "verb": op["verb"],
                "summary": op["summary"],
                "examples": op["examples"],
                "domain": op["id"].split(".", 1)[0],
                "tags": [op["id"].split(".", 1)[0]],
            }
            for op in registry["ops"]
        ],
    }


def _expand_registry(registry: dict, read_document: Any) -> dict:
    documents: dict[str, bytes] = {}

    def cached_document(name: str) -> bytes:
        if name not in documents:
            documents[name] = read_document(name)
        return documents[name]

    return {
        **registry,
        "ops": [
            {
                **op,
                **{
                    field: _expanded_spec(op[field], cached_document)
                    for field in ("params", "result")
                },
            }
            for op in registry["ops"]
        ],
    }


def _schema(
    value: dict[str, Any],
    components: dict[str, Any],
    contract_root: Path | None,
    documents: dict[str, Any],
) -> dict[str, Any]:
    if "schema" in value:
        return _curated_schema(value["schema"], components)
    if "eg_schema" in value:
        reference = value["eg_schema"]
        if not reference.startswith("contract/"):
            raise ValueError(f"invalid EG schema reference: {reference!r}")
        resource = reference.removeprefix("contract/")
        filename, pointer = _resource_parts(resource)
        if contract_root is None:
            raise ValueError(f"EG schema unavailable: {reference!r}")
        if filename not in documents:
            documents[filename] = json.loads((contract_root / filename).read_bytes())
        _pointer(documents[filename], pointer)
        return {"$ref": resource}
    raise ValueError(f"unknown schema reference: {value!r}")


def _rewrite_schema_refs(value: Any, references: dict[str, str]) -> Any:
    if not isinstance(value, dict):
        return value
    if set(value) & {"$id", "$anchor", "$dynamicRef", "$dynamicAnchor"}:
        raise ValueError("scoped schema identifiers cannot be hoisted")
    result = _map_schema_children(
        value, lambda child: _rewrite_schema_refs(child, references)
    )
    reference = value.get("$ref")
    if isinstance(reference, str):
        if reference.startswith("#") and reference not in references:
            raise ValueError("unsupported local schema reference")
        result["$ref"] = references.get(reference, reference)
    return result


def _curated_schema(schema: dict, components: dict[str, Any]) -> dict:
    definitions = schema.get("$defs", {})
    references = {
        f"#/$defs/{name}": "#/components/schemas/Definition"
        + hashlib.sha256(
            _json_bytes(_resolve_local_refs(value, schema, frozenset()))
        ).hexdigest()
        for name, value in definitions.items()
    }
    rewritten = _rewrite_schema_refs(schema, references)
    for name, value in rewritten.pop("$defs", {}).items():
        target = references[f"#/$defs/{name}"].rsplit("/", 1)[1]
        components[target] = _shared_schema(value, components)
    result = _shared_schema(rewritten, components)
    if definitions:
        # Preserve definition pointers and annotations without copying their bodies.
        result = {
            **result,
            "$defs": {
                name: {"$ref": references[f"#/$defs/{name}"]} for name in definitions
            },
        }
    return result


def _shared_schema(schema: Any, components: dict[str, Any]) -> Any:
    if not isinstance(schema, dict) or set(schema) == {"$ref"}:
        return schema
    shared = _schema_children(schema, components)
    name = "Schema" + hashlib.sha256(_json_bytes(shared)).hexdigest()
    components[name] = shared
    return {"$ref": f"#/components/schemas/{name}"}


def _schema_children(schema: dict[str, Any], components: dict[str, Any]) -> dict:
    return _map_schema_children(schema, lambda child: _shared_schema(child, components))


def _map_schema_children(schema: dict[str, Any], transform: Any) -> dict:
    result = dict(schema)
    for key in ("properties", "patternProperties", "$defs", "dependentSchemas"):
        if isinstance(schema.get(key), dict):
            result[key] = {
                name: transform(child) for name, child in schema[key].items()
            }
    for key in (
        "items",
        "additionalProperties",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "propertyNames",
        "unevaluatedProperties",
        "unevaluatedItems",
        "contentSchema",
        "additionalItems",
    ):
        if key in schema:
            result[key] = transform(schema[key])
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        if isinstance(schema.get(key), list):
            result[key] = [transform(child) for child in schema[key]]
    return result


def _http_component_uses(paths: dict):
    for methods in paths.values():
        for operation in methods.values():
            yield "requestBodies", operation, "requestBody"
            for status in operation["responses"]:
                yield "responses", operation["responses"], status


def _shared_http_components(paths: dict) -> dict:
    uses: dict[tuple[str, str], list] = {}
    for group, owner, field in _http_component_uses(paths):
        name = "Shared" + hashlib.sha256(_json_bytes(owner[field])).hexdigest()
        uses.setdefault((group, name), []).append((owner, field))
    shared: dict[str, dict] = {"requestBodies": {}, "responses": {}}
    for (group, name), targets in uses.items():
        if len(targets) > 1:
            owner, field = targets[0]
            shared[group][name] = owner[field]
            for owner, field in targets:
                owner[field] = {"$ref": f"#/components/{group}/{name}"}
    return shared


def _openapi(
    registry: dict[str, Any], contract_root: Path | None = None
) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    components: dict[str, Any] = {}
    documents: dict[str, Any] = {}
    if contract_root is None and any(
        "eg_schema" in op[field]
        for op in registry["ops"]
        for field in ("params", "result")
    ):
        contract_root = Path(
            str(importlib.resources.files("epistemic_graph") / "contract")
        )
    for op in registry["ops"]:
        http = op.get("http") or {"method": "POST", "path": f"/api/v1/ops/{op['id']}"}
        path = http["path"]
        method = http["method"].lower()
        if path in paths and method in paths[path]:
            raise ValueError(f"duplicate HTTP route: {method.upper()} {path}")
        paths.setdefault(path, {})[method] = {
            "operationId": op["id"],
            "summary": op["summary"],
            "tags": [op["id"].split(".", 1)[0]],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": _schema(
                            op["params"], components, contract_root, documents
                        )
                    }
                },
            },
            "responses": {
                "200": {
                    "description": "Operation result",
                    "content": {
                        "application/json": {
                            "schema": _schema(
                                op["result"], components, contract_root, documents
                            )
                        }
                    },
                },
                "default": {"description": "GraphOS error envelope"},
            },
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": "GraphOS API", "version": str(registry["api_version"])},
        "x-registry-digest": registry["registry_digest"],
        "paths": paths,
        "components": {"schemas": components, **_shared_http_components(paths)},
    }


def _client_models(registry: dict[str, Any]) -> bytes:
    ids = [op["id"] for op in registry["ops"]]
    lines = [
        '"""Generated operation IDs and schema lookup. Do not edit."""',
        "",
        "from __future__ import annotations",
        "",
        "import importlib.resources",
        "import json",
        "from functools import lru_cache",
        "from typing import Literal",
        "",
        "from graph_os.client._schema import _expanded_spec",
        "",
        f"REGISTRY_DIGEST = {json.dumps(registry['registry_digest'])}",
        f"API_VERSION = {json.dumps(str(registry['api_version']))}",
        "type OperationId = Literal[",
    ]
    lines.extend(f"    {json.dumps(op_id)}," for op_id in ids)
    lines.extend(
        [
            "]",
            "",
            "",
            "@lru_cache(maxsize=1)",
            "def _registry() -> dict:",
            '    source = importlib.resources.files("graph_os.api.generated") / "registry.json"',
            "    registry = json.loads(source.read_bytes())",
            '    if registry["registry_digest"] != REGISTRY_DIGEST:',
            '        raise RuntimeError("generated client registry digest mismatch")',
            "    return registry",
            "",
            "",
            "def schema_for(op_id: OperationId) -> dict:",
            '    root = importlib.resources.files("graph_os.api.generated")',
            '    for op in _registry()["ops"]:',
            '        if op["id"] == op_id:',
            "            return {",
            "                field: _expanded_spec(",
            "                    op[field], lambda name: root.joinpath(name).read_bytes()",
            "                )",
            '                for field in ("params", "result")',
            "            }",
            "    raise KeyError(op_id)",
            "",
        ]
    )
    return "\n".join(lines).encode()


def _ts_client(registry: dict[str, Any]) -> bytes:
    """Emit the digest-bound generic client consumed by agent-webui."""
    ids = " | ".join(json.dumps(op["id"]) for op in registry["ops"])
    lines = [
        "// Generated by GraphOS scripts/gen_api.py. Do not edit.",
        f"export const registryDigest = {json.dumps(registry['registry_digest'])} as const;",
        f"export const apiVersion = {json.dumps(str(registry['api_version']))} as const;",
        f"export type OperationId = {ids};",
        "export type OperationEnvelope<T = unknown> =",
        "  | { ok: true; result: T; meta: Record<string, unknown> }",
        "  | { ok: false; error: { code: string; source: string; message: string; retryable: boolean }; meta: Record<string, unknown> };",
        "export type GraphOsTransport = (opId: OperationId, params: Record<string, unknown>) => Promise<OperationEnvelope>;",
        "export function invoke<Op extends OperationId>(",
        "  transport: GraphOsTransport, opId: Op, params: Record<string, unknown>",
        "): Promise<OperationEnvelope> {",
        "  return transport(opId, params);",
        "}",
        "",
    ]
    return "\n".join(lines).encode()


def _engine_errors(registry: dict[str, Any]) -> bytes:
    from epistemic_graph.contract_errors import ContractDigestMismatch

    from graph_os.api.registry.contract_admission import (
        _installed_error_table,
        _require_digest,
        _verify_provider_receipt,
    )
    from graph_os.api.registry.eg_binding import EgContractError

    try:
        from epistemic_graph.contract import RECEIPT_DIGEST
    except ContractDigestMismatch as exc:
        raise EgContractError("cannot read provider receipt for generation") from exc

    pin = _require_digest(RECEIPT_DIGEST)
    identity = _require_digest(registry["registry_digest"])
    _verify_provider_receipt(pin)
    errors = _installed_error_table()
    _verify_provider_receipt(pin)
    lines = [
        '"""Generated engine error contract. Do not edit."""',
        "",
        f"REGISTRY_DIGEST = {json.dumps(identity)}",
        f"EG_RECEIPT_DIGEST = {json.dumps(pin)}",
        "ENGINE_ERRORS: dict[str, tuple[int, bool]] = {",
    ]
    lines.extend(
        f"    {json.dumps(code)}: {errors[code]!r}," for code in sorted(errors)
    )
    return ("\n".join([*lines, "}", ""])).encode()


def generate(registry: dict[str, Any]) -> dict[Path, bytes]:
    if not registry.get("ops"):
        raise ValueError("refusing to generate an empty API registry")
    outputs = {
        GENERATED / "engine_errors.py": _engine_errors(registry),
        GENERATED / "registry.json": _json_bytes(registry),
        GENERATED / "descriptors.json": _json_bytes(_descriptors(registry)),
        GENERATED / "__init__.py": b'''"""Generated GraphOS API contract artifacts."""

from .engine_errors import EG_RECEIPT_DIGEST, ENGINE_ERRORS, REGISTRY_DIGEST

__all__ = ["EG_RECEIPT_DIGEST", "ENGINE_ERRORS", "REGISTRY_DIGEST"]
''',
        ROOT / "docs/api/openapi.json": _json_bytes(_openapi(registry)),
        ROOT
        / "graph_os/client/__init__.py": b'"""Generated GraphOS client surface."""\n',
        ROOT / "graph_os/client/_generated_models.py": _client_models(registry),
        ROOT / "docs/api/generated.ts": _ts_client(registry),
    }
    outputs.update(_shared_schema_outputs(registry))
    return outputs


def _shared_schema_outputs(registry: dict[str, Any]) -> dict[Path, bytes]:
    outputs: dict[Path, bytes] = {}
    references = {
        _resource_parts(op[field]["eg_schema"].removeprefix("contract/"))[0]
        for op in registry["ops"]
        for field in ("params", "result")
        if "eg_schema" in op[field]
    }
    if references:
        root = importlib.resources.files("epistemic_graph").joinpath("contract")
        for filename in sorted(references):
            document = json.loads(root.joinpath(filename).read_bytes())
            content = (
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            outputs[GENERATED / filename] = content
            outputs[ROOT / "docs/api" / filename] = content
    return outputs


def _shape(spec: dict[str, Any], *, result: bool) -> dict[str, Any]:
    shape = spec.get("schema", spec.get("schema_snapshot", {}))
    if result and "bodies" in shape:
        return shape.get("bodies", {}).get("result", {}).get("schema", {})
    return shape


def _shape_breaks(
    old: dict[str, Any], new: dict[str, Any], *, path: str, result: bool
) -> list[str]:
    changes: list[str] = []
    required = set(new.get("required", [])) - set(old.get("required", []))
    if not result and required:
        changes.append(f"new required params {path}: {sorted(required)}")
    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    removed = set(old_props) - set(new_props)
    if removed:
        kind = "result fields" if result else "params"
        changes.append(f"removed {kind} {path}: {sorted(removed)}")
    old_enum, new_enum = old.get("enum"), new.get("enum")
    if old_enum and new_enum and not set(old_enum) <= set(new_enum):
        changes.append(f"narrowed enum {path}")
    old_type, new_type = old.get("type"), new.get("type")
    allows_numeric_widening = not result and (old_type, new_type) == (
        "integer",
        "number",
    )
    if old_type != new_type and not allows_numeric_widening:
        changes.append(f"changed type {path}")
    for name in old_props.keys() & new_props.keys():
        changes.extend(
            _shape_breaks(
                old_props[name], new_props[name], path=f"{path}.{name}", result=result
            )
        )
    return changes


def _breaking_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Identify contract changes that existing stable callers cannot absorb."""
    current = {op["id"]: op for op in after["ops"]}
    changes: list[str] = []
    for previous in before["ops"]:
        if previous.get("stability") != "stable":
            continue
        op_id = previous["id"]
        updated = current.get(op_id)
        if updated is None:
            changes.append(f"{op_id}: removed")
            continue
        for field in ("params", "result"):
            old_spec, new_spec = previous[field], updated[field]
            old_shape = _shape(old_spec, result=field == "result")
            new_shape = _shape(new_spec, result=field == "result")
            findings = _shape_breaks(
                old_shape, new_shape, path=field, result=field == "result"
            )
            changes.extend(f"{op_id}: {finding}" for finding in findings)
            old_hash, new_hash = (
                old_spec.get("schema_sha256"),
                new_spec.get("schema_sha256"),
            )
            if (old_hash is not None or new_hash is not None) and old_hash != new_hash:
                if not findings:
                    changes.append(
                        f"{op_id}: EG {field} schema changed; review required"
                    )
    return changes


def _check_compat(registry: dict[str, Any], base_ref: str) -> int:
    git = shutil.which("git")
    if git is None:
        print(
            "gen_api: git is not on PATH; cannot check compatibility", file=sys.stderr
        )
        return 1
    subprocess.run(  # nosec B603 -- resolved git executable, fixed argv
        [git, "rev-parse", "--verify", base_ref],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    baseline = subprocess.run(  # nosec B603 -- resolved git executable, fixed argv
        [git, "show", f"{base_ref}:graph_os/api/generated/registry.json"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if baseline.returncode:
        print("gen_api: no baseline generated registry at base ref (first release)")
        return 0
    previous = json.loads(baseline.stdout)

    def baseline_document(filename: str) -> bytes:
        result = subprocess.run(  # nosec B603 -- fixed git argv, validated schema resource
            [git, "show", f"{base_ref}:graph_os/api/generated/{filename}"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        return result.stdout

    previous = _expand_registry(previous, baseline_document)
    provider = importlib.resources.files("epistemic_graph").joinpath("contract")
    current = _expand_registry(
        registry, lambda name: provider.joinpath(name).read_bytes()
    )
    changes = _breaking_changes(previous, current)
    if not changes:
        return 0
    old_major = int(str(previous["api_version"]).split(".", 1)[0])
    new_major = int(str(registry["api_version"]).split(".", 1)[0])
    changelog = ROOT / "docs/api/CHANGELOG.md"
    text = changelog.read_text() if changelog.exists() else ""
    if new_major > old_major and all(row.split(":", 1)[0] in text for row in changes):
        return 0
    for row in changes:
        print(f"breaking API change: {row}", file=sys.stderr)
    print(
        "gen_api: stable breaking changes require a major bump and changelog entry",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-compat", metavar="BASE_REF")
    parser.add_argument("--registry-module", default="graph_os.api.ops")
    args = parser.parse_args()
    try:
        registry = _registry(args.registry_module)
        if args.check_compat:
            return _check_compat(registry, args.check_compat)
        outputs = generate(registry)
    except (
        AttributeError,
        ImportError,
        OSError,
        subprocess.CalledProcessError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"gen_api: {exc}", file=sys.stderr)
        return 2
    drift = [
        path
        for path, content in outputs.items()
        if not path.exists() or path.read_bytes() != content
    ]
    if args.check:
        for path in drift:
            print(f"out of date: {path.relative_to(ROOT)}", file=sys.stderr)
        return int(bool(drift))
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
