"""The external graph mapping policy an analysis reads from its declaration: bounded JSON, the policy fields and their resolved form (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from typing import Any

from arbora.api.errors import not_served
from arbora.core.settings import RuntimeConfig

logger = logging.getLogger(__name__)


_EXTERNAL_MAPPING_POLICY_FIELDS = frozenset(
    {
        "access",
        "edge_property_allowlist",
        "edge_type_overrides",
        "identity_property",
        "property_allowlist",
        "type_overrides",
    }
)


_EXTERNAL_MAPPING_POLICY_MAX_BYTES = 4 * 1024 * 1024


def _reject_nonfinite_json(_value: str) -> None:
    raise ValueError("non-finite JSON constants are not supported")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON keys are not supported")
        value[key] = item
    return value


def _as_declaration_dict(configured: Any) -> dict[str, Any]:
    if hasattr(configured, "model_dump"):
        return configured.model_dump(exclude_defaults=True)
    if isinstance(configured, dict):
        return dict(configured)
    return {}


def _last_declaration_named(declarations: Any, name: str) -> dict[str, Any] | None:
    """The LAST entry in ``declarations`` whose ``name`` matches, or ``None``
    -- matches the original scan's "later entries win" behaviour exactly."""
    found: dict[str, Any] | None = None
    for configured in declarations:
        candidate = _as_declaration_dict(configured)
        if str(candidate.get("name") or "") == name:
            found = candidate
    return found


def _configured_external_graph_declaration(
    name: str, config: RuntimeConfig
) -> dict[str, Any]:
    """Resolve one reference-only declaration without returning it publicly."""
    collections = (
        getattr(config, "external_graph_connectors", []) or [],
        getattr(config, "kg_connections", []) or [],
    )
    selected: dict[str, Any] = {}
    for declarations in collections:
        found = _last_declaration_named(declarations, name)
        if found is not None:
            selected = found
    return selected


def _load_external_mapping_policy_ref(store: Any, policy_ref: str) -> dict[str, Any]:
    """Resolve+parse the policy behind ``policy_ref``. An empty ref (no
    policy attached) resolves to an empty policy."""
    if not policy_ref:
        return {}
    raw = store.resolve_ref(policy_ref)
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw.encode("utf-8")) > _EXTERNAL_MAPPING_POLICY_MAX_BYTES
    ):
        raise ValueError("external mapping policy is missing or exceeds its bound")

    parsed = json.loads(
        raw,
        parse_constant=_reject_nonfinite_json,
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    if not isinstance(parsed, dict):
        raise ValueError("external mapping policy must be an object")
    return parsed


def _validate_external_mapping_policy_fields(policy: dict[str, Any]) -> None:
    if set(policy).difference(_EXTERNAL_MAPPING_POLICY_FIELDS):
        raise ValueError("external mapping policy contains unsupported inline material")
    for field in ("access", "type_overrides", "edge_type_overrides"):
        if field in policy and not isinstance(policy[field], dict):
            raise ValueError("external mapping policy has an invalid mapping field")


def _validate_external_mapping_policy_access(policy: dict[str, Any]) -> None:
    access = policy.get("access")
    if not isinstance(access, dict):
        return
    if set(access).difference({"group_ids", "is_public", "markings"}):
        raise ValueError(
            "external mapping policy access cannot contain inline identities"
        )
    if "is_public" in access and not isinstance(access["is_public"], bool):
        raise ValueError("external mapping policy access is invalid")
    for field in ("group_ids", "markings"):
        value = access.get(field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise ValueError("external mapping policy access is invalid")


def _validate_external_mapping_policy_type_overrides(policy: dict[str, Any]) -> None:
    for field in ("type_overrides", "edge_type_overrides"):
        value = policy.get(field)
        if isinstance(value, dict) and any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError("external mapping policy has an invalid type mapping")


def _validate_external_mapping_policy_allowlists(policy: dict[str, Any]) -> None:
    for field in ("property_allowlist", "edge_property_allowlist"):
        value = policy.get(field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise ValueError("external mapping policy has an invalid allowlist")


def _validate_external_mapping_policy_identity(policy: dict[str, Any]) -> None:
    if "identity_property" in policy and not isinstance(
        policy["identity_property"], str
    ):
        raise ValueError("external mapping policy has an invalid identity property")


def _resolved_external_mapping_policy(
    store: Any, declaration: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Resolve, validate, and hash a property-graph policy behind its ref."""

    policy_ref = str(declaration.get("mapping_policy_ref") or "")
    policy = _load_external_mapping_policy_ref(store, policy_ref)

    _validate_external_mapping_policy_fields(policy)
    _validate_external_mapping_policy_access(policy)
    _validate_external_mapping_policy_type_overrides(policy)
    _validate_external_mapping_policy_allowlists(policy)
    _validate_external_mapping_policy_identity(policy)

    raise not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )
