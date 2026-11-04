"""Canonical, byte-stable registry projection and SHA-256 digest."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

from .spec import EgSchemaRef, OpSpec

Catalog = Mapping[str, Any]


class _CatalogView:
    """The catalog digest and method IDs, resolved once and only when needed."""

    def __init__(self, catalog: Catalog | None) -> None:
        self._catalog = catalog
        self._resolved: tuple[str, frozenset[str]] | None = None

    def resolve(self) -> tuple[str, frozenset[str]]:
        if self._resolved is None:
            from .eg_binding import load_catalog

            checked = load_catalog(self._catalog)
            methods = frozenset(
                str(row.get("id"))
                for row in checked["methods"]
                if isinstance(row, Mapping)
            )
            self._resolved = (str(checked["catalog_digest"]), methods)
        return self._resolved


def _eg_schema(model: EgSchemaRef, view: _CatalogView) -> dict[str, str]:
    digest, methods = view.resolve()
    if model.method not in methods:
        raise ValueError(f"EG method absent from the catalog: {model.path!r}")
    return {"eg_schema": model.path, "catalog_digest": digest}


def _schema(model: type[BaseModel] | EgSchemaRef, view: _CatalogView) -> dict[str, Any]:
    if isinstance(model, EgSchemaRef):
        return _eg_schema(model, view)
    return {
        "model": f"{model.__module__}.{model.__qualname__}",
        "schema": model.model_json_schema(),
    }


def _canonical_op(op: OpSpec, view: _CatalogView) -> dict[str, Any]:
    data = op.model_dump(mode="json", exclude={"params", "result"})
    data["params"] = _schema(op.params, view)
    data["result"] = _schema(op.result, view)
    data["scopes"] = sorted(op.scopes)
    data["executor_scopes"] = sorted(op.executor_scopes)
    data["surfaces"] = sorted(op.surfaces)
    return data


def canonical_op(op: OpSpec, *, catalog: Catalog | None = None) -> dict[str, Any]:
    """Project every wire- or authority-relevant declaration field.

    An engine-bound side digests as its method reference plus the catalog
    digest, so a descriptor change moves the registry digest. ``catalog`` is
    for fixtures; ``None`` reads the installed engine's run-time catalog.
    """

    return _canonical_op(op, _CatalogView(catalog))


def canonical_registry(
    ops: Iterable[OpSpec], *, api_version: str = "1", catalog: Catalog | None = None
) -> bytes:
    """Return the canonical UTF-8 registry document used by generators and runtime."""

    view = _CatalogView(catalog)
    rows = [_canonical_op(op, view) for op in sorted(ops, key=lambda item: item.id)]
    return json.dumps(
        {"api_version": api_version, "ops": rows},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def registry_digest(
    ops: Iterable[OpSpec], *, api_version: str = "1", catalog: Catalog | None = None
) -> str:
    return hashlib.sha256(
        canonical_registry(ops, api_version=api_version, catalog=catalog)
    ).hexdigest()
