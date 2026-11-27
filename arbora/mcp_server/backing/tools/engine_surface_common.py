"""Helpers shared by the engine-surface tool families: error shaping, client resolution and invocation (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from arbora.security.error_surface import public_error_payload

logger = logging.getLogger(__name__)


# ── Transport resolution (reuse the one engine client; injectable for tests) ──
def _client(graph: str) -> Any:
    """Resolve the shared ``SyncMedullaClient`` for ``graph``.

    Delegates to :func:`engine_tools._client_for` so these tools ride the exact
    same connect path (CONCEPT:MERISTEM-OS.deployment.engine-resolver-auto-provision resolver, connection caching) as the
    low-level ``engine_<domain>`` tools — one transport, no reinvention. Isolated
    behind this thin indirection so unit tests can inject a mock client
    (CONCEPT:MERISTEM-KG.coordination.engine-message-broker).
    """
    from arbora.mcp_server.backing.tools import engine_tools

    return engine_tools._client_for(graph)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, bytes | bytearray):
        return {"__bytes_b64__": base64.b64encode(bytes(obj)).decode("ascii")}
    return str(obj)


def _degraded(surface: str, action: str, tried: list[str]) -> str:
    """Clean 'this engine build lacks the surface' payload (never raise)."""
    return json.dumps(
        {
            "surface": surface,
            "action": action,
            "degraded": True,
            "error": (
                f"engine surface {surface!r} is not available in this engine build "
                "(no matching client method); this tool degrades cleanly and will "
                "activate once the engine ships the capability"
            ),
            "tried": tried,
        }
    )


def _surface_error(
    exc: BaseException,
    *,
    surface: str,
    action: str = "",
    code: str = "operation_failed",
    context: dict[str, Any] | None = None,
) -> str:
    """Return a stable protocol failure without copying UNTRUSTED request metadata.

    ``surface``/``action`` (and any ``context`` extras, e.g. ``delegated``/
    ``available``) are values the CALLER already supplied to this same
    invocation — not exception-controlled or otherwise untrusted data — so
    merging them into the redacted payload lets a caller correlate which
    surface/action failed (matching the success path's
    ``{"surface", "action", "result"}`` shape) without reintroducing anything
    :func:`~arbora.security.error_surface.public_error_payload`
    exists to keep out: the raw exception text/args, which stays fully
    redacted (only ``type(exc).__name__`` is ever logged or returned).
    """

    payload = public_error_payload(exc, code=code, context=context)
    if context:
        payload.update(context)
    payload["surface"] = surface
    payload["action"] = action
    return json.dumps(payload)


def _resolve(client: Any, candidates: tuple[tuple[str, str], ...]) -> Any:
    """Return the first callable ``client.<sub>.<method>`` among ``candidates``.

    Returns ``None`` when none of the candidate surfaces are present so the caller
    can degrade gracefully (CONCEPT:MERISTEM-KG.coordination.engine-message-broker).
    """
    for sub_attr, meth_attr in candidates:
        sub = getattr(client, sub_attr, None)
        if sub is None:
            continue
        fn = getattr(sub, meth_attr, None)
        if callable(fn):
            return fn
    return None


def _invoke(
    *,
    surface: str,
    action: str,
    graph: str,
    candidates: tuple[tuple[str, str], ...],
    params: dict[str, Any],
) -> str:
    """Resolve the client, dispatch to the first present surface, JSON the result.

    Every failure mode is returned as data, never raised: engine unreachable →
    ``error``; surface absent → ``degraded``; bad kwargs / engine error →
    ``error`` (CONCEPT:MERISTEM-KG.coordination.engine-message-broker).
    """
    try:
        client = _client(graph)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "engine down is a normal degrade", exc_info=True
        )
        return _surface_error(
            exc,
            surface=surface,
            action=action,
            code="dependency_unavailable",
        )
    fn = _resolve(client, candidates)
    if fn is None:
        # ".".join(...) rather than an f-string: these are diagnostic
        # attribute-path names for a "degraded" status message (never a
        # query), but the two-part dotted shape is otherwise indistinguishable
        # from a schema-qualified table/label composition at the AST level.
        return _degraded(surface, action, [".".join((a, m)) for a, m in candidates])
    return _invoke_callable(fn, surface=surface, action=action, params=params)


def _invoke_callable(
    fn: Any, *, surface: str, action: str, params: dict[str, Any]
) -> str:
    """Invoke one resolved engine method and render the shared surface envelope."""
    try:
        result = fn(**params)
    except TypeError as exc:
        return _surface_error(
            exc, surface=surface, action=action, code="invalid_request"
        )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "surface engine errors as data", exc_info=True
        )
        return _surface_error(exc, surface=surface, action=action)
    return json.dumps(
        {"surface": surface, "action": action, "result": result}, default=_json_default
    )


def _drop_empty(**kwargs: Any) -> dict[str, Any]:
    """Keep only kwargs the caller actually supplied (non-empty string / non-None)."""
    return {k: v for k, v in kwargs.items() if v not in ("", None)}


def _decode_json_object(
    raw: str, surface: str, *, action: str = "", field: str = "params_json"
) -> tuple[Any, str | None]:
    """Decode a JSON-object tool argument, or return the rejection to send.

    Returns ``(decoded, None)`` on success and ``(None, error_json)`` otherwise —
    the same two rejections every action-routed tool in this module already
    produced by hand: an ``invalid_request`` surface error for undecodable text,
    and a plain ``{surface[, action], error}`` payload for a non-object.
    """
    try:
        decoded = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as exc:
        return None, _surface_error(
            exc, surface=surface, action=action, code="invalid_request"
        )
    if isinstance(decoded, dict):
        return decoded, None
    rejection: dict[str, Any] = {"surface": surface}
    if action:
        rejection["action"] = action
    rejection["error"] = f"{field} must decode to an object"
    return None, json.dumps(rejection)
