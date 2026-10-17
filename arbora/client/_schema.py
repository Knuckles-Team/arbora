"""Shared validation and expansion of packaged provider schemas."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def _pointer(document: Any, reference: str) -> Any:
    if not reference.startswith("/"):
        raise ValueError(f"invalid EG schema pointer: {reference!r}")
    value = document
    for token in reference.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or token not in value:
            raise ValueError(f"missing EG schema pointer: {reference!r}")
        value = value[token]
    return value


def _resolve_local_refs(value: Any, document: dict, seen: frozenset[str]) -> Any:
    if isinstance(value, list):
        return [_resolve_local_refs(item, document, seen) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        return _resolve_reference(value, document, seen)
    return {
        key: _resolve_local_refs(item, document, seen) for key, item in value.items()
    }


def _resolve_reference(value: dict, document: dict, seen: frozenset[str]) -> Any:
    reference = value["$ref"]
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError("unsafe schema reference")
    if reference in seen:
        return value
    target = _pointer(document, reference[1:])
    resolved = _resolve_local_refs(target, document, seen | {reference})
    siblings = {key: item for key, item in value.items() if key != "$ref"}
    if not siblings:
        return resolved
    return {**resolved, **_resolve_local_refs(siblings, document, seen)}


def _resource_parts(reference: str) -> tuple[str, str]:
    filename, separator, pointer = reference.partition("#")
    if not separator or not re.fullmatch(r"schemas/[A-Za-z0-9_.-]+\.json", filename):
        raise ValueError(f"unsafe schema resource: {reference!r}")
    if not pointer.startswith("/"):
        raise ValueError(f"invalid schema pointer: {reference!r}")
    return filename, pointer


def _expanded_spec(spec: dict, read_document: Any) -> dict:
    snapshot = spec.get("schema_snapshot")
    if not isinstance(snapshot, dict) or "$ref" not in snapshot:
        return spec
    reference = snapshot["$ref"]
    if not isinstance(reference, str) or set(snapshot) != {"$ref"}:
        raise ValueError("invalid schema snapshot reference")
    filename, pointer = _resource_parts(reference)
    if spec.get("eg_schema") != "contract/" + reference:
        raise ValueError("schema snapshot differs from provider reference")
    document = json.loads(read_document(filename))
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != spec.get("schema_sha256"):
        raise ValueError("schema resource digest mismatch")
    selected = _pointer(document, pointer)
    expanded = _resolve_local_refs(selected, document, frozenset())
    return {**spec, "schema_snapshot": expanded}
