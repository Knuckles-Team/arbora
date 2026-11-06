"""Verified fleet, live protocol, and SDK entries as one catalog source."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast

from arbora.fleet.catalog_items import (
    CatalogItem,
    CredentialMode,
    connector_items,
    items_from_child_probe,
    items_from_eg_catalog,
)

VerifiedRead = Callable[[], Awaitable[Any]]
LiveRead = Callable[[], Awaitable[Mapping[str, Mapping[str, Any]]]]
SdkRead = Callable[[], Awaitable[Iterable[Mapping[str, Any]]]]


@dataclass(frozen=True, slots=True)
class _ServerPolicy:
    required_scopes: frozenset[str]
    executor_scopes: frozenset[str]
    credential_mode: CredentialMode
    fleet_effects: Mapping[str, str]


def _decode_manifest_config(body: bytes) -> dict[str, Any]:
    """Decode and unwrap the engine-verified manifest body, no probe input."""
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("fleet manifest exceeds boundary")
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("fleet manifest is not JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("fleet manifest must be an object")
    nested = decoded.get("config", decoded.get("mcpServer", decoded))
    if not isinstance(nested, dict):
        raise ValueError("fleet manifest config must be an object")
    return nested


def _decode_credential_mode(
    nested: Mapping[str, Any],
    raw_scopes: frozenset[str],
    executor_scopes: frozenset[str],
) -> CredentialMode:
    mode = nested.get("credential_mode", "delegated")
    if not isinstance(mode, str) or mode not in {"delegated", "service"}:
        raise ValueError("fleet credential_mode is invalid")
    if mode == "service" and not raw_scopes:
        raise ValueError("service child requires declared domain scopes")
    if mode == "service" and not executor_scopes:
        raise ValueError("service child requires executor_scopes")
    return cast(CredentialMode, mode)


def _decode_fleet_effects(nested: Mapping[str, Any]) -> Mapping[str, str]:
    effects = nested.get("fleet_effects", {})
    if (
        not isinstance(effects, dict)
        or len(effects) > 2_048
        or any(
            not isinstance(name, str)
            or not 1 <= len(name) <= 256
            or not isinstance(value, str)
            or value not in {"read", "write", "destructive", "admin"}
            for name, value in effects.items()
        )
    ):
        raise ValueError("fleet_effects are invalid")
    return cast(Mapping[str, str], effects)


def _verified_server_policy(body: bytes) -> _ServerPolicy:
    """Read only engine-verified server content, never live probe metadata."""
    nested = _decode_manifest_config(body)
    raw_scopes = _scopes(nested.get("required_scopes", ()), "required_scopes")
    executor_scopes = _scopes(nested.get("executor_scopes", ()), "executor_scopes")
    mode = _decode_credential_mode(nested, raw_scopes, executor_scopes)
    effects = _decode_fleet_effects(nested)
    return _ServerPolicy(raw_scopes, executor_scopes, mode, effects)


def _scopes(value: Any, field_name: str) -> frozenset[str]:
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, list | tuple) or any(
        not isinstance(scope, str) or not 1 <= len(scope) <= 128 for scope in value
    ):
        raise ValueError(f"fleet {field_name} are invalid")
    return frozenset(value)


def _admitted_policies(snapshot: Any) -> dict[str, _ServerPolicy]:
    return {
        server.component.server_name: _verified_server_policy(server.content.body)
        for server in snapshot.servers
        if server.registration is not None
    }


def _admitted_subjects(snapshot: Any) -> dict[str, Any]:
    return {
        server.component.server_name: getattr(server.component, "component_id", None)
        for server in snapshot.servers
        if server.registration is not None
    }


def _require_service_subjects(
    admitted: Mapping[str, _ServerPolicy], subjects: Mapping[str, Any]
) -> None:
    if any(
        not isinstance(subjects[name], str) or not subjects[name]
        for name, policy in admitted.items()
        if policy.credential_mode == "service"
    ):
        raise ValueError("service child lacks verified EG subject_id")


def _eg_merged_items(
    snapshot: Any, admitted: Mapping[str, _ServerPolicy], subjects: Mapping[str, Any]
) -> dict[str, CatalogItem]:
    merged: dict[str, CatalogItem] = {}
    for item in items_from_eg_catalog(snapshot):
        server = item.server
        if server is None or server not in admitted:
            continue
        policy = admitted[server]
        merged[item.id] = replace(
            item,
            required_scopes=policy.required_scopes,
            credential_mode=policy.credential_mode,
            executor_scopes=policy.executor_scopes,
            subject_id=subjects[server],
            effect_override=policy.fleet_effects.get(item.name),
        )
    return merged


def _merge_probed_item(
    merged: dict[str, CatalogItem],
    item: CatalogItem,
    *,
    policy: _ServerPolicy,
    subject_id: Any,
) -> None:
    previous = merged.get(item.id)
    if previous is None:
        merged[item.id] = replace(
            item,
            required_scopes=policy.required_scopes,
            credential_mode=policy.credential_mode,
            executor_scopes=policy.executor_scopes,
            subject_id=subject_id,
            effect_override=policy.fleet_effects.get(item.name),
        )
    elif item.kind == "tool":
        # A live probe refines the engine's tool contract (ARBORA-FLEET-R072);
        # a field the probe lacks keeps the engine's value.
        merged[item.id] = replace(
            previous,
            schema=item.schema or previous.schema,
            schema_fingerprint=item.schema_fingerprint or previous.schema_fingerprint,
            annotations=(
                item.annotations
                if item.annotations is not None
                else previous.annotations
            ),
        )


#: Probe families whose answer is the server's whole current set of a kind,
#: with the ``catalog_family_errors`` key that voids it (``None``: only the
#: probe's own ``error`` does). Resources are left out: the engine names a
#: pack resource by its entry name and the probe by its URI, so absence from
#: the probe does not prove a resource was withdrawn.
_AUTHORITATIVE_FAMILIES: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    ("tools", None, ("tool",)),
    ("native_prompts", "prompts", ("prompt",)),
    ("skills", "resources", ("skill",)),
)


def _answered_kinds(info: Mapping[str, Any]) -> set[str]:
    """The item kinds one good probe answered in full (ARBORA-FLEET-R070.1)."""
    if info.get("error"):
        return set()
    errors = info.get("catalog_family_errors") or {}
    return {
        kind
        for family, error_key, kinds in _AUTHORITATIVE_FAMILIES
        if family in info and (error_key is None or error_key not in errors)
        for kind in kinds
    }


def _drop_unserved(
    merged: dict[str, CatalogItem],
    server: str,
    info: Mapping[str, Any],
    served: set[str],
) -> None:
    """Drop the server's items a full probe no longer lists.

    ARBORA-FLEET-R070.1: a refresh replaces a server's set; a tool, prompt or
    skill the engine still holds from an older pack but the server stopped
    serving is not offered or admitted.
    """
    kinds = _answered_kinds(info)
    stale = [
        item_id
        for item_id, item in merged.items()
        if item.server == server and item.kind in kinds and item_id not in served
    ]
    for item_id in stale:
        del merged[item_id]


def _merge_probed_items(
    merged: dict[str, CatalogItem],
    admitted: Mapping[str, _ServerPolicy],
    subjects: Mapping[str, Any],
    probe: Mapping[str, Mapping[str, Any]],
) -> None:
    for server, policy in admitted.items():
        info = probe.get(server, {})
        probed = items_from_child_probe(server, info)
        for item in probed:
            _merge_probed_item(merged, item, policy=policy, subject_id=subjects[server])
        if info:
            _drop_unserved(merged, server, info, {item.id for item in probed})


def _merge_sdk_items(
    merged: dict[str, CatalogItem], entries: Iterable[Mapping[str, Any]]
) -> None:
    for item in connector_items(entries):
        if item.id in merged:
            raise ValueError(f"duplicate connector item: {item.id}")
        merged[item.id] = item


class CombinedFleetSource:
    """Only Medulla-admitted live servers may contribute probed protocol rows.

    EG owns component identity and body. A child probe may fill a tool schema
    or advertise a protocol family that EG does not model, but can never make
    an unregistered server visible. SDK entries arrive through a separate
    typed adapter; the caller supplies its verified pack reader.
    """

    def __init__(self, *, verified: VerifiedRead, live: LiveRead, sdk: SdkRead):
        self._verified = verified
        self._live = live
        self._sdk = sdk

    async def __call__(self) -> tuple[CatalogItem, ...]:
        snapshot = await self._verified()
        admitted = _admitted_policies(snapshot)
        subjects = _admitted_subjects(snapshot)
        _require_service_subjects(admitted, subjects)
        merged = _eg_merged_items(snapshot, admitted, subjects)
        probe = await self._live()
        _merge_probed_items(merged, admitted, subjects, probe)
        _merge_sdk_items(merged, await self._sdk())
        return tuple(merged[name] for name in sorted(merged))
