"""The outbound A2A peer registry, served as operations (ARBORA-A2A-R001.7).

``a2a.peer.register``, ``a2a.peer.delete`` and ``a2a.peer.list`` keep the peers this
tenant may call. Peers persist in ``~/.config/arbora/a2a_peers.yml`` under the key
``peers``. A peer holds ``name``, ``endpoint``, ``card_digest``, ``skills``,
``credential_ref`` and ``refreshed_at``; the credential is only ever a reference.
Register replaces the record of the same name, so it is its own replay owner; a
refresh that changes the card digest appends a revision (caller and time), and the
newest 50 are kept. A foreign tenant's peer answers exactly like an absent one.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.registry import OpSpec

#: The replay owner of each write: the registry file replaces or removes by name, so
#: a repeated request leaves the same state and appends no second revision.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "a2a.peer.register": "a2a_peers:replace-by-name",
        "a2a.peer.delete": "a2a_peers:delete-by-name",
    }
)
PEERS_FILE = "a2a_peers.yml"
MAX_REVISIONS = 50
_REFERENCE = re.compile(r"^(openbao|vault|env|secret):[A-Za-z0-9_./-]{1,200}$")
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_LOCK = threading.Lock()


class PeerRegisterParams(_common.Params):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    endpoint: str = Field(min_length=1, max_length=2048)
    card_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    skills: list[str] = Field(default_factory=list, max_length=256)
    credential_ref: str = Field(min_length=1, max_length=256)
    #: The topic prefixes this peer may be sent (ARBORA-A2A-R024); none by default.
    topic_scope: list[str] = Field(default_factory=list, max_length=64)


class PeerDeleteParams(_common.Params):
    name: str = Field(min_length=1, max_length=128)


class PeerListParams(_common.Params):
    pass


class PeerResult(_common.Params):
    value: dict[str, Any]


def _refuse(code: str, **details: Any) -> OperationRefused:
    return OperationRefused(code, details)


def _validate(request: PeerRegisterParams) -> None:
    if not _REFERENCE.fullmatch(request.credential_ref):
        # Only a reference to a secret is stored; a value-shaped string is refused.
        raise _refuse("PEER_CREDENTIAL_VALUE")
    parts = urlsplit(request.endpoint)
    host = (parts.hostname or "").lower()
    if not parts.netloc or (
        parts.scheme != "https" and not (parts.scheme == "http" and host in _LOOPBACK)
    ):
        raise _refuse("PEER_ENDPOINT_INSECURE")


class PeerRegistry:
    """The peer file, read once per change and indexed by ``(tenant, name)``."""

    def __init__(
        self,
        path: Callable[[], Path],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = path
        self._clock = clock
        self._stamp: tuple[int, int] | None = None
        self._index: dict[tuple[str, str], dict[str, Any]] = {}

    def _load(self) -> dict[tuple[str, str], dict[str, Any]]:
        import yaml

        target = self._path()
        try:
            stat = target.stat()
        except FileNotFoundError:
            self._stamp, self._index = None, {}
            return self._index
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._stamp:
            document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            rows = document.get("peers", []) if isinstance(document, dict) else []
            self._index = {
                (str(row["tenant"]), str(row["name"])): dict(row)
                for row in rows
                if isinstance(row, Mapping) and "tenant" in row and "name" in row
            }
            self._stamp = stamp
        return self._index

    def _save(self, index: Mapping[tuple[str, str], Mapping[str, Any]]) -> None:
        import yaml

        target = self._path()
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = [dict(index[key]) for key in sorted(index)]
        handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            yaml.safe_dump({"peers": rows}, stream, sort_keys=True)
        os.replace(temporary, target)
        self._stamp = None

    def lookup(self, tenant: str, name: str) -> dict[str, Any] | None:
        """One peer of ``tenant``; another tenant's peer is an absent one."""
        return self._load().get((tenant, name))

    def list(self, tenant: str) -> list[dict[str, Any]]:
        """The tenant's peers in name order, without their revision history."""
        index = self._load()
        return [_public(index[key]) for key in sorted(index) if key[0] == tenant]

    def register(
        self, tenant: str, caller: str, request: PeerRegisterParams
    ) -> dict[str, Any]:
        now = self._clock().isoformat()
        with _LOCK:
            index = dict(self._load())
            prior = index.get((tenant, request.name))
            revisions = list(prior.get("revisions", [])) if prior else []
            if prior is None or prior.get("card_digest") != request.card_digest:
                revisions.append(
                    {"card_digest": request.card_digest, "caller": caller, "at": now}
                )
            record = {
                "tenant": tenant,
                "name": request.name,
                "endpoint": request.endpoint,
                "card_digest": request.card_digest,
                "skills": sorted(set(request.skills)),
                "credential_ref": request.credential_ref,
                "topic_scope": sorted(set(request.topic_scope)),
                "refreshed_at": now,
                "revisions": revisions[-MAX_REVISIONS:],
            }
            index[(tenant, request.name)] = record
            self._save(index)
        return _public(record)

    def delete(self, tenant: str, name: str) -> bool:
        with _LOCK:
            index = dict(self._load())
            removed = index.pop((tenant, name), None) is not None
            if removed:
                self._save(index)
        return removed


def _public(record: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "name",
        "endpoint",
        "card_digest",
        "skills",
        "credential_ref",
        "refreshed_at",
    )
    return {key: record[key] for key in keys} | {
        "topic_scope": list(record.get("topic_scope", [])),
        "revisions": list(record.get("revisions", [])),
    }


def _default_path() -> Path:
    from arbora.core.settings import config_dir

    return config_dir() / PEERS_FILE


_REGISTRY = PeerRegistry(_default_path)


def _registry(context: Any) -> PeerRegistry:
    return context.services.get("a2a_peers") or _REGISTRY


async def handle_peer_register(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = PeerRegisterParams.model_validate(dict(params))
    _validate(request)
    caller = context.caller
    return {
        "value": _registry(context).register(caller.tenant, caller.principal, request)
    }


async def handle_peer_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = PeerDeleteParams.model_validate(dict(params))
    if not _registry(context).delete(context.caller.tenant, request.name):
        # A foreign tenant's peer and an absent one answer alike.
        raise _refuse("NOT_FOUND")
    return {"value": {"deleted": request.name}}


async def handle_peer_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    PeerListParams.model_validate(dict(params))
    return {"value": {"peers": _registry(context).list(context.caller.tenant)}}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_write_op(
            op_id="a2a.peer.register",
            summary="Register or refresh an outbound A2A peer by name",
            examples=("register an A2A peer", "refresh a peer's agent card"),
            params=PeerRegisterParams,
            result=PeerResult,
            handler="arbora.api.ops.a2a_peers.handle_peer_register",
            scope="registry:write",
        ),
        _common.build_write_op(
            op_id="a2a.peer.delete",
            summary="Remove an outbound A2A peer",
            examples=("remove an A2A peer",),
            params=PeerDeleteParams,
            result=PeerResult,
            handler="arbora.api.ops.a2a_peers.handle_peer_delete",
            scope="registry:write",
        ),
        _common.build_read_op(
            op_id="a2a.peer.list",
            summary="List the outbound A2A peers of this tenant in name order",
            examples=("list A2A peers",),
            params=PeerListParams,
            result=PeerResult,
            handler="arbora.api.ops.a2a_peers.handle_peer_list",
            scope="registry:read",
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "PeerRegistry",
    "handle_peer_delete",
    "handle_peer_list",
    "handle_peer_register",
    "operations",
    "specs",
]
