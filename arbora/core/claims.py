"""Immutable JSON-shaped authority, shared by the config shape and the invoke steps."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = ["claim_values", "freeze_claims", "params_digest"]


def freeze_claims(value: Any) -> Any:
    """Own immutable JSON-shaped authority; never retain caller aliases."""
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_claims(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_claims(child) for child in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError("authority must contain only immutable JSON values")


def _freeze_mapping(value: Mapping[Any, Any]) -> Mapping[str, Any]:
    if any(not isinstance(key, str) for key in value):
        raise ValueError("authority keys must be strings")
    return MappingProxyType({key: freeze_claims(child) for key, child in value.items()})


def claim_values(value: Any) -> Any:
    """Copy frozen authority into the public client's JSON carrier."""
    if isinstance(value, Mapping):
        return {key: claim_values(child) for key, child in value.items()}
    if isinstance(value, (tuple, frozenset)):
        return [claim_values(child) for child in value]
    return value


def params_digest(params: Mapping[str, Any]) -> str:
    raw = json.dumps(
        claim_values(params),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()
