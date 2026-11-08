"""Small in-memory method catalogs in the shape ``medulla.engine.method_catalog()`` serves."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def method_row(
    method_id: str,
    scope: str,
    *,
    domain: str = "query",
    mutates: bool = False,
    idempotent: bool = True,
    replay_class: str = "NotReplayable",
    is_wire_callable: bool = True,
    stability: str = "stable",
) -> dict[str, Any]:
    return {
        "id": method_id,
        "domain": domain,
        "is_wire_callable": is_wire_callable,
        "error_set": [],
        "policy": {
            "authz_action": scope,
            "mutates": mutates,
            "idempotent": idempotent,
            "audited": False,
            "emits_cdc": False,
            "durability_domain": "graph",
            "txn_participation": "none",
        },
        "replay_class": replay_class,
        "consumer_profiles": [],
        "stability": stability,
        "note": method_id,
    }


def scope_row(scope: str, scope_class: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "class": scope_class,
        "owner": "medulla",
        "approver_group": None,
    }


def catalog(
    methods: Iterable[dict[str, Any]],
    scopes: Iterable[dict[str, Any]],
    *,
    digest: str = "c" * 64,
) -> dict[str, Any]:
    return {
        "catalog_digest": digest,
        "methods": list(methods),
        "scopes": list(scopes),
    }
