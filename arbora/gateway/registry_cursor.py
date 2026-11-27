"""The registry page cursor: how a next-page token is minted, bound to its caller and decoded (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException

from arbora.security.run_token import RunTokenSigner, TokenError

logger = logging.getLogger(__name__)


_MAX_CURSOR_BYTES = 4096


_CURSOR_TTL_SECONDS = 900.0


def _parse_kind_cursors(params: Any, kinds: list[str]) -> dict[str, str]:
    """Parse one ``cursor_<kind>`` query parameter per requested kind --
    never a single combined cursor, since each kind's keyset position is
    independent."""
    cursors: dict[str, str] = {}
    for kind in kinds:
        cursor = params.get(f"cursor_{kind}") or None
        if cursor is None:
            continue
        if len(cursor.encode("utf-8")) > _MAX_CURSOR_BYTES:
            raise HTTPException(status_code=400, detail="invalid registry cursor")
        cursors[kind] = cursor
    return cursors


def _cursor_token(
    signer: RunTokenSigner,
    *,
    kind: str,
    query: str,
    after: tuple[str, str],
    tenant: str,
    principal: str,
    grant_digests: tuple[str, ...] = (),
    grant_digest: str | None = None,
) -> str:
    """Mint an existing HMAC run token bound to the registry read scope."""

    if grant_digest is not None:
        grant_digests = (grant_digest,)
    grant_digests = tuple(sorted(set(grant_digests)))
    payload = json.dumps(
        {
            "kind": kind,
            "query": query,
            "after": list(after),
            "grant_digests": list(grant_digests),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return signer.mint(
        payload,
        project="registry",
        endpoints=("registry",),
        operations=("read",),
        ttl_seconds=_CURSOR_TTL_SECONDS,
        actor_id=principal,
        tenant_id=tenant,
    )


def _decode_cursor(
    signer: RunTokenSigner,
    token: str,
    *,
    kind: str,
    query: str,
    tenant: str,
    principal: str,
    grant_digests: tuple[str, ...],
) -> tuple[str, str]:
    """Verify cursor integrity and bind it to this tenant/principal/filter."""

    try:
        decoded = signer.validate(token, endpoint="registry", operation="read")
        _check_cursor_authority(decoded, tenant=tenant, principal=principal)
        payload = json.loads(decoded.run_id)
        _check_cursor_binding(
            payload, kind=kind, query=query, grant_digests=grant_digests
        )
        return _cursor_after_position(payload)
    except (TokenError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid registry cursor") from exc


def _check_cursor_authority(decoded: Any, *, tenant: str, principal: str) -> None:
    if decoded.tenant_id != tenant or decoded.actor_id != principal:
        raise TokenError("cursor authority mismatch")


def _check_cursor_binding(
    payload: dict[str, Any],
    *,
    kind: str,
    query: str,
    grant_digests: tuple[str, ...],
) -> None:
    payload_grants = payload.get("grant_digests")
    if not isinstance(payload_grants, list) or not all(
        isinstance(value, str) and value for value in payload_grants
    ):
        raise TokenError("cursor grant binding is malformed")
    if (
        payload.get("kind") != kind
        or payload.get("query") != query
        or tuple(sorted(set(payload_grants))) != tuple(sorted(set(grant_digests)))
    ):
        raise TokenError("cursor query mismatch")


def _cursor_after_position(payload: dict[str, Any]) -> tuple[str, str]:
    after = payload.get("after")
    if (
        not isinstance(after, list)
        or len(after) != 2
        or any(not isinstance(value, str) for value in after)
    ):
        raise TokenError("cursor position is malformed")
    return after[0], after[1]
