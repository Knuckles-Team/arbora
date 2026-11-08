"""Recover native identities only from EG's immutable owner-bound admission."""

from __future__ import annotations

import json
from typing import Any


def require_lookup(namespace: Any) -> Any:
    """Fail before submission when the installed producer cannot prove ownership."""
    from arbora.api.invoke.pipeline import OperationRefused

    lookup = getattr(namespace, "get_run", None)
    try:
        from medulla.generated.models import RunWorkItemView
    except ImportError as exc:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "native_run_admission"}
        ) from exc
    if not callable(lookup) or not hasattr(RunWorkItemView, "model_validate_json"):
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "native_run_admission"}
        )
    return lookup


def _identity(request: dict[str, Any]) -> str:
    context = dict(request["context"])
    context.pop("issued_at_ms", None)
    context.pop("expires_at_ms", None)
    return json.dumps(
        {**request, "context": context}, sort_keys=True, separators=(",", ":")
    )


def recover(lookup: Any, request: dict[str, Any]) -> tuple[str, bool] | None:
    """The lookup is taken directly from the configured engine namespace."""
    from medulla.generated.models import RunWorkItemView, SubmitWorkItemRequest

    from arbora.api.invoke.pipeline import OperationRefused

    value = lookup(tenant=request["context"]["tenant_id"], run_id=request["run_id"])
    if value is None:
        return None
    # Validate the entire response, not just the identity or mutable metadata.
    view = RunWorkItemView.model_validate_json(json.dumps(value), strict=True)
    admission = view.admission.model_dump(mode="json")
    expected = SubmitWorkItemRequest.model_validate_json(
        json.dumps(request), strict=True
    ).model_dump(mode="json")
    if _identity(admission) != _identity(expected):
        raise OperationRefused(
            "IDEMPOTENCY_CONFLICT", {"capability": "native_run_admission"}
        )
    item_id = view.work_item.work_item_id
    if not item_id or item_id != item_id.strip():
        raise ValueError("native run admission has invalid engine identity")
    return item_id, False


def submit(namespace: Any, request: dict[str, Any]) -> tuple[str, bool]:
    """Lookup first; a lost submit response permits one read, never a replay."""
    lookup = require_lookup(namespace)
    existing = recover(lookup, request)
    if existing is not None:
        return existing
    try:
        result = namespace.submit(request)
    except Exception as exc:
        if (
            not isinstance(exc, (TimeoutError, ConnectionError, OSError))
            and getattr(exc, "code", None) != "IDEMPOTENCY_CONFLICT"
        ):
            raise
        existing = recover(lookup, request)
        if existing is None:
            raise
        return existing
    return str(result["work_item_id"]), bool(result["created"])
