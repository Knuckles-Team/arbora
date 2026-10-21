"""Native EG lease binding for the reference-bound tool-task worker."""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Mapping
from typing import Any

from arbora import work_items
from arbora.api.contracts.session import resolve_session

from .tool_task import WorkItemLeaseLost


class EgWorkItemFence:
    """Use EG's WorkItem authority under the current verified session.

    This adapter owns no durable state and supplies no execution permission.
    Its opaque claim holds the native fence, not a replacement lease.
    """

    def __init__(self, engine: Any, *, tenant: str, lease_seconds: float = 60) -> None:
        if not tenant or not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("tenant and positive finite lease duration required")
        self._engine = engine
        self._tenant = tenant
        self._lease_seconds = lease_seconds
        self._worker = f"arbora-tool-task-{secrets.token_hex(16)}"

    def _session(self) -> None:
        session = resolve_session(required_scope="kg:write")
        if session.tenant != self._tenant:
            raise WorkItemLeaseLost("tool-task tenant changed")

    def get(self, item_id: str) -> Mapping[str, Any] | None:
        """Project the native caller-scoped record for the worker precheck."""
        self._session()
        row = work_items.get_work_item(self._engine, item_id)
        if row is None:
            return None
        if row.get("work_item_id") != item_id:
            raise WorkItemLeaseLost("tool-task record identity changed")
        if row.get("status") in {"dead_letter", "cancelled"}:
            raise WorkItemLeaseLost("tool-task effect requires owner reconciliation")
        return {
            **row,
            "payload_ref": row.get("input_ref"),
            "tenant": self._tenant,
        }

    def claim(self, item_id: str) -> str | None:
        """Acquire only through the native WorkItem claim transition."""
        self._session()
        admitted = self.get(item_id)
        if admitted is None or admitted.get("status") != "ready":
            return None
        claim = work_items.claim_specific(
            self._engine,
            item_id,
            token=self._worker,
            lease_ttl_s=self._lease_seconds,
        )
        if claim is None:
            return None
        if (
            type(claim.get("attempt")) is not int
            or claim["attempt"] != 1
            or claim.get("work_item_id") != item_id
            or claim.get("payload_ref") != admitted.get("payload_ref")
        ):
            return None
        return json.dumps({"item_id": item_id, "claim": claim}, sort_keys=True)

    def _claim(self, item_id: str, token: str) -> Mapping[str, Any]:
        self._session()
        try:
            value = json.loads(token)
            claim = value["claim"]
            if (
                value["item_id"] != item_id
                or claim["tenant"] != self._tenant
                or claim["lease_holder_ref"] != self._worker
            ):
                raise ValueError("claim identity changed")
        except (ValueError, TypeError, KeyError):
            raise WorkItemLeaseLost("invalid tool-task claim") from None
        return claim

    def require_current(self, item_id: str, claim: str) -> None:
        """Renew the exact native fence; an unsuccessful renewal stops work."""
        native = self._claim(item_id, claim)
        if not work_items.heartbeat(
            self._engine, item_id, native, lease_ttl_s=self._lease_seconds
        ):
            raise WorkItemLeaseLost("tool-task lease renewal refused")

    def commit(self, item_id: str, claim: str, *, succeeded: bool, ref: str) -> None:
        """Accept only a native commit of this exact terminal outcome."""
        native = self._claim(item_id, claim)
        result = work_items.commit_result(
            self._engine,
            item_id,
            native,
            outcome="succeeded" if succeeded else "failed",
            result_ref=ref if succeeded else None,
            error_ref=None if succeeded else ref,
        )
        if result != "committed":
            raise WorkItemLeaseLost("tool-task result commit refused")
