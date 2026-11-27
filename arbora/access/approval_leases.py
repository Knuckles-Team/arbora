"""Action approvals as engine ``action.approval`` control leases.

The engine owns the approval record (ARBORA-IDENTITY-R019.8/.9). It reserves
``ActionApproval`` rows for its native control-lease path and refuses a
generic node write of one, so an approval is filed with ``IssueControlLease``,
listed with ``ListControlLeases`` and decided with ``TransitionControlLease``:
``active`` is pending, ``consumed`` is approved, ``revoked`` is denied.

The synchronous callers (the action policy, the fleet routes, the governance
tool) reach the engine through ``engine.control_leases``: a blocking view
whose ``tenant`` is the verified session's tenant and whose ``issue``,
``get``, ``list`` and ``transition`` take the engine client's keyword
arguments. The served ``approvals.*`` operations use the async client and the
same :func:`approval_lease_to_props` projection.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

__all__ = [
    "ACTION_APPROVAL_KIND",
    "APPROVAL_ID_PREFIX",
    "APPROVAL_LIFETIME_MS",
    "ApprovalLeasesUnavailable",
    "approval_lease_to_props",
    "decide_approval",
    "file_approval",
    "granted_approval",
    "pending_approvals",
]

#: The control-lease ``kind`` of an action approval. The engine's governed
#: schema repair reads approvals of this same kind.
ACTION_APPROVAL_KIND = "action.approval"
#: Every approval lease id starts with this prefix.
APPROVAL_ID_PREFIX = "action_approval:"
#: An approval lives 24 hours from filing: the engine's maximum lease span.
APPROVAL_LIFETIME_MS = 24 * 60 * 60 * 1000
#: Most pages one listing follows; a page holds at most 100 leases.
_MAX_PAGES = 64
#: Lifecycle fields every control lease view carries. The lease ``kind`` is
#: projected as ``lease_kind``: ``kind`` stays the held action's kind.
_LEASE_FIELDS = (
    "lease_id",
    "status",
    "revision",
    "issued_at_ms",
    "expires_at_ms",
    "hard_expires_at_ms",
)
_TARGET_BY_DECISION = MappingProxyType({"approved": "consumed", "denied": "revoked"})


class ApprovalLeasesUnavailable(RuntimeError):
    """The engine handle exposes no control-lease view."""


def approval_lease_to_props(lease: Mapping[str, Any]) -> dict[str, Any]:
    """Project one ``action.approval`` lease to its public properties.

    Starts from the grant (the held request: ``kind`` is the action kind),
    then layers the lifecycle fields, ``lease_kind`` and ``id`` (the lease
    id) on top. Never mutates ``lease``.
    """
    grant = lease.get("grant")
    props: dict[str, Any] = dict(grant) if isinstance(grant, Mapping) else {}
    for field_name in _LEASE_FIELDS:
        if field_name in lease:
            props[field_name] = lease[field_name]
    if "kind" in lease:
        props["lease_kind"] = lease["kind"]
    if "lease_id" in lease:
        props["id"] = lease["lease_id"]
    return props


def _leases(engine: Any) -> Any:
    leases = getattr(engine, "control_leases", None) if engine is not None else None
    if leases is None:
        raise ApprovalLeasesUnavailable("the engine exposes no control-lease view")
    return leases


def _unexpired(lease: Mapping[str, Any], now_ms: int) -> bool:
    try:
        deadline = min(int(lease["expires_at_ms"]), int(lease["hard_expires_at_ms"]))
    except (KeyError, TypeError, ValueError):
        return False
    return now_ms < deadline


def _approvals(
    leases: Any, *, status: str, grant_match: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Every ``action.approval`` lease of one status, following the cursor."""
    found: list[dict[str, Any]] = []
    cursor = None
    for _ in range(_MAX_PAGES):
        page = leases.list(
            tenant=leases.tenant,
            kind=ACTION_APPROVAL_KIND,
            status=status,
            grant_match=None if grant_match is None else dict(grant_match),
            cursor=cursor,
            limit=100,
        )
        found.extend(
            lease
            for lease in page.get("leases") or ()
            if lease.get("kind") == ACTION_APPROVAL_KIND
        )
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return found


def file_approval(engine: Any, grant: Mapping[str, Any], *, now_ms: int) -> str:
    """File one pending approval for ``grant`` and return its lease id.

    An unexpired pending approval of the same ``request_digest`` is reused.
    A failed dedup read does not block filing.

    Raises:
        ApprovalLeasesUnavailable: the engine has no control-lease view.
        RuntimeError: the engine answered anything but ``issued``.
        Exception: the engine's refusal, unchanged.
    """
    leases = _leases(engine)
    digest = str(grant["request_digest"])
    try:
        pending = _approvals(
            leases, status="active", grant_match={"request_digest": digest}
        )
    except Exception:
        logging.getLogger(__name__).debug(
            "dedup is best-effort; filing still runs", exc_info=True
        )
        pending = []
    for lease in pending:
        if _unexpired(lease, now_ms):
            return str(lease["lease_id"])
    lease_id = f"{APPROVAL_ID_PREFIX}{uuid.uuid4().hex}"
    answer = leases.issue(
        tenant=leases.tenant,
        lease_id=lease_id,
        kind=ACTION_APPROVAL_KIND,
        grant=dict(grant),
        issued_at_ms=now_ms,
        expires_at_ms=now_ms + APPROVAL_LIFETIME_MS,
        hard_expires_at_ms=now_ms + APPROVAL_LIFETIME_MS,
        idempotency_key=lease_id,
    )
    if not isinstance(answer, Mapping) or answer.get("outcome") != "issued":
        raise RuntimeError("the engine did not issue the approval lease")
    return lease_id


def granted_approval(engine: Any, request_digest: str, *, now_ms: int) -> str | None:
    """The id of a consumed, unexpired approval of exactly ``request_digest``."""
    leases = _leases(engine)
    for lease in _approvals(
        leases, status="consumed", grant_match={"request_digest": request_digest}
    ):
        grant = lease.get("grant") or {}
        if grant.get("request_digest") == request_digest and _unexpired(lease, now_ms):
            return str(lease["lease_id"])
    return None


def pending_approvals(engine: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    """Up to ``limit`` pending approvals, projected to public properties."""
    pending = _approvals(_leases(engine), status="active")
    return [approval_lease_to_props(lease) for lease in pending[:limit]]


def decide_approval(engine: Any, approval_id: str, decision: str) -> None:
    """Approve (``consumed``) or deny (``revoked``) one pending approval.

    Raises:
        ValueError: not an approval id, or not ``approved``/``denied``.
        LookupError: missing, another kind, no longer pending, or the
            transition did not apply.
    """
    if not str(approval_id).startswith(APPROVAL_ID_PREFIX):
        raise ValueError("approval_id must identify an action approval")
    target = _TARGET_BY_DECISION.get(decision)
    if target is None:
        raise ValueError("decision must be approved or denied")
    leases = _leases(engine)
    lease = leases.get(tenant=leases.tenant, lease_id=str(approval_id))
    if not isinstance(lease, Mapping) or lease.get("kind") != ACTION_APPROVAL_KIND:
        raise LookupError("action approval is unavailable")
    if lease.get("status") != "active":
        raise LookupError("action approval is no longer pending")
    answer = leases.transition(
        tenant=leases.tenant,
        lease_id=str(approval_id),
        expected_revision=int(lease["revision"]),
        to=target,
        idempotency_key=f"decide:{approval_id}",
    )
    if not isinstance(answer, Mapping) or answer.get("outcome") != "applied":
        raise LookupError("approval is missing or no longer pending")
