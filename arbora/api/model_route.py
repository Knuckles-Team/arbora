"""The tenant model route of a role run (ARBORA-OPS-R919).

A role run prefers the tenant's local model: an endpoint that speaks the common
chat completion protocol and that the tenant owns. The route is one
tenant-shared engine record (``model.route``), so Arbora holds no route table
and no escalation policy. A hosted model serves a run only when the record's
escalation policy names the role and the reason, and the run carries the
policy id, the reason and the route it used. Arbora never falls back to a
hosted model silently, and the probe sends no tenant content.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.pin_guards import refusal
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods

NAMESPACE = "model.route"
KEY = "route"
PROBE_TIMEOUT_S = 3.0
Probe = Callable[[str], Awaitable[bool]]


async def http_probe(endpoint: str) -> bool:
    """Whether the endpoint answers its model list; the request carries no content."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
            answer = await client.get(endpoint.rstrip("/") + "/models")
    except httpx.HTTPError:
        return False
    return answer.status_code < 500


async def _record(context: Any) -> Mapping[str, Any] | None:
    try:
        require_methods("KvGet")
    except PinLagUnavailable as error:
        raise refusal(error) from error
    entry = await engine_send(
        context, "KvGet", {"namespace": NAMESPACE, "key": KEY, "shared": True}
    )
    raw = entry.get("value") if isinstance(entry, Mapping) else None
    if isinstance(raw, (list, tuple)):
        raw = bytes(raw)
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = json.loads(bytes(raw))
        except ValueError:
            return None
    return raw if isinstance(raw, Mapping) and raw else None


def _allows(policy: Any, role: str, reason: str) -> bool:
    return (
        isinstance(policy, Mapping)
        and bool(policy.get("policy_id"))
        and role in (policy.get("roles") or ())
        and reason in (policy.get("reasons") or ())
    )


async def resolve_route(
    context: Any,
    role_instance_id: str,
    escalate: Mapping[str, Any] | None = None,
    *,
    probe: Probe = http_probe,
) -> dict[str, Any] | None:
    """The route a role run uses, or ``None`` when the tenant set no route."""
    record = await _record(context)
    if record is None:
        return None
    policy = record.get("escalation")
    if escalate is not None:
        reason = str(escalate.get("reason") or "")
        if not _allows(policy, role_instance_id, reason):
            raise OperationRefused("ESCALATION_NOT_ALLOWED")
        return _hosted(policy, reason)
    local = record.get("local")
    endpoint = local.get("endpoint") if isinstance(local, Mapping) else None
    if isinstance(endpoint, str) and endpoint and await probe(endpoint):
        return {"route": "local", "policy_id": None, "reason": None}
    if _allows(policy, role_instance_id, "local_unavailable"):
        return _hosted(policy, "local_unavailable")
    raise OperationRefused("LOCAL_MODEL_UNAVAILABLE")


def _hosted(policy: Any, reason: str) -> dict[str, Any]:
    return {"route": "hosted", "policy_id": str(policy["policy_id"]), "reason": reason}


__all__ = ["http_probe", "resolve_route"]
