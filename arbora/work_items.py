"""Arbora seam for durable WorkItems, bound to the engine WorkItem methods.

Each verb calls one served engine method through the engine client's
``work_items`` namespace (``SubmitWorkItem``, ``GetWorkItem``,
``ClaimWorkItem``, ``RenewWorkItemLease``, ``CommitWorkItemResult``,
``DeferWorkItem``, ``CancelWorkItem``). Arbora maps arguments and results
here and nowhere else. The engine owns dedupe, leases, fencing and terminal
state; the verified session supplies the tenant and request context.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

from arbora.security.anonymous import session_context

#: The engine's spec row that owns durable WorkItem transitions.
SPEC_ROW = "EG-INGEST-WORKER-R605"

#: Statuses after which a WorkItem never transitions again.
TERMINAL_WORK_ITEM_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "dead_letter"}
)

_MAX_TENANT_IN_FLIGHT = 4096


class WorkItemBackendUnavailable(RuntimeError):
    """The engine WorkItem authority cannot serve the transition."""


class NativeWorkItemRequired(WorkItemBackendUnavailable):
    """The engine-native WorkItem method is required and not bound."""


def _session() -> Any:
    from arbora.api.contracts.session import current_session

    session = current_session()
    if session is None:
        raise WorkItemBackendUnavailable("a verified session is required")
    return session


def _tenant(tenant: str | None = None) -> str:
    current = str(_session().tenant)
    if tenant is not None and tenant != current:
        raise WorkItemBackendUnavailable("tenant differs from the verified session")
    return current


def _work_items(engine: Any) -> Any:
    namespace = getattr(engine, "work_items", None)
    if namespace is None:
        raise NativeWorkItemRequired("the engine client has no work_items namespace")
    return namespace


def _now_ms(now: float | None = None) -> int:
    return int((time.time() if now is None else now) * 1000)


def _key(*parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _typed_request_context(
    claims: Mapping[str, Any], session: Any, key: str
) -> dict[str, Any]:
    """EG's typed ``RequestContext`` (v2) for one WorkItem request.

    ``SubmitWorkItem`` refuses the bare authority claims; this is the same
    context the effect journal sends, on the control graph that holds
    WorkItems.
    """
    if "tenant_id" in claims:
        return dict(claims)  # already EG's typed context
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.mcp_server.effect_journal import _request_context

    class _Graph:
        graph_name = CONTROL_GRAPH

    return _request_context(
        {
            "auth_mode": claims.get("auth_mode", "authenticated"),
            "agent_id": claims.get("agent_id") or claims.get("principal"),
            "principal": claims.get("principal"),
            "tenant": claims.get("tenant") or getattr(session, "tenant", ""),
            "audience": claims.get("audience") or getattr(session, "audience", ""),
            "policy_version": claims.get("policy_version")
            or getattr(session, "policy_version", ""),
            "scopes": list(
                claims.get("scopes") or getattr(session, "scopes", ()) or ()
            ),
        },
        _Graph(),
        key,
    )


def submit_work_item_atomic(
    engine: Any,
    *,
    kind: str,
    idempotency_key: str,
    tenant: str | None = None,
    work_item_id: str | None = None,
    queue: str = "",
    resource_class: str = "",
    fairness_group: str = "",
    payload_ref: str = "",
    max_attempts: int = 1,
    priority: int = 0,
    description: str = "",
    created_by: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> tuple[str, bool]:
    """Submit one WorkItem (``SubmitWorkItem``); return its id and creation."""
    tenant_id = _tenant(tenant)
    session = _session()
    labels = {
        "queue": queue,
        "resource_class": resource_class,
        "fairness_group": fairness_group,
        "description": description,
    }
    body = {**{k: v for k, v in labels.items() if v}, **dict(metadata or {})}
    command_digest = _key(tenant_id, kind, work_item_id, payload_ref, body)
    claims = dict(session_context(session))
    context = _typed_request_context(claims, session, idempotency_key)
    # EG refuses empty digests (``SubmitWorkItem.policy_digest must be a
    # non-empty trimmed string``): bind the policy version, the command and
    # "none" for a WorkItem that pins no model, as the effect journal does.
    policy_version = str(
        claims.get("policy_version") or getattr(session, "policy_version", "") or "none"
    )
    request = {
        "schema_version": "1",
        "context": context,
        "work_item_id": work_item_id,
        "idempotency_key": idempotency_key,
        "command_digest": command_digest,
        "kind": kind,
        "priority": int(priority),
        "depends_on": [],
        "input_ref": payload_ref or f"arbora-work-item:{command_digest}",
        "policy_digest": hashlib.sha256(policy_version.encode()).hexdigest(),
        "catalog_digest": command_digest,
        "model_digest": "none",
        "max_attempts": int(max_attempts),
        "deadline_unix": None,
        "metadata": body,
        "provenance_refs": [created_by] if created_by else [],
        "max_tenant_in_flight": _MAX_TENANT_IN_FLIGHT,
    }
    result = _work_items(engine).submit(request)
    return str(result["work_item_id"]), bool(result["created"])


def get_work_item(engine: Any, item_id: str) -> dict[str, Any] | None:
    """Read the caller's view of one WorkItem (``GetWorkItem``)."""
    return _work_items(engine).get(tenant=_tenant(), work_item_id=item_id)


def claim_specific(
    engine: Any,
    item_id: str,
    *,
    token: str,
    lease_ttl_s: float,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Lease one WorkItem (``ClaimWorkItem``); ``None`` when not granted."""
    tenant_id = _tenant()
    answer = _work_items(engine).claim(
        {
            "schema_version": "1",
            "tenant_ref": tenant_id,
            "work_item_id": item_id,
            "queue_ref": None,
            "resource_class": None,
            "fairness_group": None,
            "worker_ref": token,
            "now_ms": _now_ms(now),
            "lease_ms": max(1, int(lease_ttl_s * 1000)),
            "max_tenant_in_flight": _MAX_TENANT_IN_FLIGHT,
        }
    )
    if not answer.get("claimed"):
        return None
    return {**answer, "tenant": tenant_id}


def _fence(claim: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tenant": _tenant(claim.get("tenant")),
        "worker_id": str(claim.get("lease_holder_ref", "")),
        "lease_epoch": int(claim.get("lease_epoch", 0)),
        "fencing_token": int(claim.get("fencing_token", 0)),
    }


def heartbeat(
    engine: Any, item_id: str, claim: Mapping[str, Any], *, lease_ttl_s: float
) -> bool:
    """Renew a held lease (``RenewWorkItemLease``)."""
    result = _work_items(engine).renew(
        work_item_id=item_id,
        now_ms=_now_ms(),
        lease_ms=max(1, int(lease_ttl_s * 1000)),
        **_fence(claim),
    )
    return bool(result.get("renewed"))


def commit_result(
    engine: Any,
    item_id: str,
    claim: Mapping[str, Any],
    *,
    outcome: str,
    result_ref: str | None = None,
    error_ref: str | None = None,
    retryable: bool = False,
) -> str:
    """Commit an outcome (``CommitWorkItemResult``).

    Return ``committed`` when the requested outcome applied, ``noop`` when the
    WorkItem was already terminal, and the engine status otherwise.
    """
    fence = _fence(claim)
    result = _work_items(engine).commit_result(
        work_item_id=item_id,
        idempotency_key=_key("commit", item_id, fence["fencing_token"], outcome),
        outcome=outcome,
        now_ms=_now_ms(),
        result_ref=result_ref,
        error_ref=error_ref,
        retryable=retryable,
        **fence,
    )
    status = str(result.get("status"))
    return "committed" if status == outcome else status


def defer_work_item(
    engine: Any,
    item_id: str,
    claim: Mapping[str, Any],
    *,
    next_retry_at: float,
    reason_ref: str | None = None,
) -> bool:
    """Return a held WorkItem to ``ready`` (``DeferWorkItem``)."""
    fence = _fence(claim)
    result = _work_items(engine).defer(
        work_item_id=item_id,
        idempotency_key=_key("defer", item_id, fence["fencing_token"]),
        next_retry_at_ms=_now_ms(next_retry_at),
        now_ms=_now_ms(),
        reason_ref=reason_ref,
        **fence,
    )
    return result.get("status") == "deferred"


def cancel_work_item(engine: Any, item_id: str, *, reason: str | None = None) -> bool:
    """Cancel a pending WorkItem (``CancelWorkItem``)."""
    result = _work_items(engine).cancel(
        tenant=_tenant(),
        work_item_id=item_id,
        idempotency_key=_key("cancel", item_id),
        now_ms=_now_ms(),
        reason_ref=reason,
    )
    return result.get("status") == "cancelled"


__all__ = [
    "SPEC_ROW",
    "TERMINAL_WORK_ITEM_STATUSES",
    "NativeWorkItemRequired",
    "WorkItemBackendUnavailable",
    "cancel_work_item",
    "claim_specific",
    "commit_result",
    "defer_work_item",
    "get_work_item",
    "heartbeat",
    "submit_work_item_atomic",
]
