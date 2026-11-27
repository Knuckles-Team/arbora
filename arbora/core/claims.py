"""Immutable JSON-shaped authority, shared by the config shape and the invoke steps."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = ["freeze_claims"]


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
