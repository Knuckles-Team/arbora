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
from typing import TYPE_CHECKING, Any

from arbora.security.anonymous import session_context

if TYPE_CHECKING:
    from epistemic_graph.generated.models import ConversationTurnAdmission

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


def native_owner_graph(engine: Any) -> str:
    """Read routing only from the engine that submits and claims the WorkItem."""
    graph = getattr(engine, "work_item_graph", None)
    if graph is None:
        graph = getattr(engine, "graph_name", None)
    if not isinstance(graph, str) or not graph or graph != graph.strip():
        raise NativeWorkItemRequired("native WorkItem graph binding is unavailable")
    return graph


def _now_ms(now: float | None = None) -> int:
    return int((time.time() if now is None else now) * 1000)


def _key(*parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _authority_context(claims: Mapping[str, Any], session: Any) -> dict[str, Any]:
    """Fill missing authority claims from the verified session only."""
    return {
        "auth_mode": claims.get("auth_mode", "authenticated"),
        "agent_id": claims.get("agent_id") or claims.get("principal"),
        "principal": claims.get("principal"),
        "tenant": claims.get("tenant") or getattr(session, "tenant", ""),
        "audience": claims.get("audience") or getattr(session, "audience", ""),
        "policy_version": claims.get("policy_version")
        or getattr(session, "policy_version", ""),
        "scopes": list(claims.get("scopes") or getattr(session, "scopes", ()) or ()),
    }


def _typed_request_context(
    claims: Mapping[str, Any], session: Any, key: str, graph: str | None = None
) -> dict[str, Any]:
    """EG's typed ``RequestContext`` (v2) for one WorkItem request.

    ``SubmitWorkItem`` refuses the bare authority claims; this is the same
    context the effect journal sends, on the control graph that holds
    WorkItems.
    """
    if "tenant_id" in claims:
        if graph is not None and claims.get("graph") != graph:
            raise NativeWorkItemRequired("native WorkItem context graph mismatch")
        return dict(claims)  # already EG's typed context
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.mcp_server.effect_journal import _request_context

    class _Graph:
        graph_name = graph if graph is not None else CONTROL_GRAPH

    return _request_context(
        _authority_context(claims, session),
        _Graph(),
        key,
    )


def _require_native_submission(run_id: str | None, work_item_id: str | None) -> None:
    """Refuse native ownership before admission unless the contract supports it."""
    if run_id is not None:
        from epistemic_graph.generated.models import SubmitWorkItemRequest

        from arbora.api.invoke.pipeline import OperationRefused

        if "run_id" not in SubmitWorkItemRequest.model_fields:
            raise OperationRefused(
                "UPGRADE_REQUIRED", {"capability": "native_run_owner"}
            )
        if work_item_id is not None:
            raise ValueError("native run ownership requires engine-assigned identity")


def _policy_digest(claims: Mapping[str, Any], session: Any) -> str:
    """Bind a nonempty policy digest, matching the effect journal default."""
    version = str(
        claims.get("policy_version") or getattr(session, "policy_version", "") or "none"
    )
    return hashlib.sha256(version.encode()).hexdigest()


def _conversation_admission(
    conversation: ConversationTurnAdmission | None, run_id: str | None
) -> dict[str, Any] | None:
    """Validate a host-supplied native carrier without inventing conversation order."""
    if conversation is None:
        return None
    from arbora.api.invoke.pipeline import OperationRefused

    try:
        from epistemic_graph.generated.models import (
            ConversationTurnAdmission,
            SubmitWorkItemRequest,
        )
    except ImportError as exc:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "conversation_admission"}
        ) from exc
    if "conversation" not in SubmitWorkItemRequest.model_fields:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "conversation_admission"}
        )
    if run_id is None:
        raise ValueError("conversation admission requires native run ownership")
    if not isinstance(conversation, ConversationTurnAdmission):
        raise TypeError("generated ConversationTurnAdmission is required")
    return ConversationTurnAdmission.model_validate_json(
        conversation.model_dump_json(), strict=True
    ).model_dump(mode="json")


def _submission_digest(
    tenant_id: str,
    kind: str,
    work_item_id: str | None,
    payload_ref: str,
    body: Mapping[str, Any],
    run_id: str | None,
    turn: Any,
) -> str:
    """Bind optional native identity and immutable conversation into the command."""
    command_digest = _key(tenant_id, kind, work_item_id, payload_ref, body)
    if run_id is not None:
        command_digest = _key(command_digest, run_id)
    if turn is not None:
        command_digest = _key(command_digest, turn)
    return command_digest


def submit_work_item_atomic(
    engine: Any,
    *,
    kind: str,
    idempotency_key: str,
    tenant: str | None = None,
    work_item_id: str | None = None,
    run_id: str | None = None,
    queue: str = "",
    resource_class: str = "",
    fairness_group: str = "",
    payload_ref: str = "",
    max_attempts: int = 1,
    priority: int = 0,
    description: str = "",
    created_by: str = "",
    metadata: Mapping[str, Any] | None = None,
    conversation: ConversationTurnAdmission | None = None,
) -> tuple[str, bool]:
    """Submit one WorkItem (``SubmitWorkItem``); return its id and creation."""
    _require_native_submission(run_id, work_item_id)
    turn = _conversation_admission(conversation, run_id)
    tenant_id = _tenant(tenant)
    session = _session()
    labels = {
        "queue": queue,
        "resource_class": resource_class,
        "fairness_group": fairness_group,
        "description": description,
    }
    body = {**{k: v for k, v in labels.items() if v}, **dict(metadata or {})}
    command_digest = _submission_digest(
        tenant_id, kind, work_item_id, payload_ref, body, run_id, turn
    )
    claims = dict(session_context(session))
    context = _typed_request_context(
        claims,
        session,
        idempotency_key,
        native_owner_graph(engine) if run_id is not None else None,
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
        "policy_digest": _policy_digest(claims, session),
        "catalog_digest": command_digest,
        "model_digest": "none",
        "max_attempts": int(max_attempts),
        "deadline_unix": None,
        "metadata": body,
        "provenance_refs": [created_by] if created_by else [],
        "max_tenant_in_flight": _MAX_TENANT_IN_FLIGHT,
    }
    if turn is not None:
        request["conversation"] = turn
    if run_id is not None:
        request["run_id"] = run_id
    if run_id is not None:
        from arbora.native_run_admission import submit

        return submit(_work_items(engine), request)
    return _submit_request(engine, request, run_id)


def _submit_request(
    engine: Any, request: dict[str, Any], run_id: str | None
) -> tuple[str, bool]:
    """Translate an older producer's native-owner refusal without fallback."""
    try:
        result = _work_items(engine).submit(request)
    except ValueError as exc:
        if run_id is not None and "unsupported fields: run_id" in str(exc):
            from arbora.api.invoke.pipeline import OperationRefused

            raise OperationRefused(
                "UPGRADE_REQUIRED", {"capability": "native_run_owner"}
            ) from exc
        raise
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
    completed_text_history: bool = False,
) -> dict[str, Any] | None:
    """Lease one WorkItem (``ClaimWorkItem``); ``None`` when not granted."""
    if type(completed_text_history) is not bool:
        raise TypeError("completed history opt-in must be boolean")
    options = {}
    if completed_text_history:
        try:
            from epistemic_graph.generated.models import MethodClaimWorkItemParams

            if "completed_text_history" not in MethodClaimWorkItemParams.model_fields:
                raise AttributeError("completed history claim opt-in")
        except (ImportError, AttributeError) as exc:
            raise NativeWorkItemRequired(
                "completed history claim schema unavailable"
            ) from exc
        options["completed_text_history"] = True
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
        },
        **options,
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
    run_completion: Mapping[str, Any] | None = None,
) -> str:
    """Commit an outcome (``CommitWorkItemResult``).

    Return ``committed`` when the requested outcome applied, ``noop`` when the
    WorkItem was already terminal, and the engine status otherwise.
    """
    completion = {}
    if run_completion is not None:
        from epistemic_graph.generated.models import RunCompletionInput

        completion["run_completion"] = RunCompletionInput.model_validate_json(
            json.dumps(dict(run_completion)), strict=True
        ).model_dump(mode="json", exclude_none=True)
    fence = _fence(claim)
    result = _work_items(engine).commit_result(
        work_item_id=item_id,
        idempotency_key=_key("commit", item_id, fence["fencing_token"], outcome),
        outcome=outcome,
        now_ms=_now_ms(),
        result_ref=result_ref,
        error_ref=error_ref,
        retryable=retryable,
        **completion,
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
