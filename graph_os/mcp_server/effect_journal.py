"""The pipeline's durable effect owner, held by EG's served WorkItem methods.

A governed child call (``fleet.call``) must be reserved durably before it is
dispatched, at most once per idempotency key, and its outcome must be
replayable. EG owns that state: one effect is one EG WorkItem.

* ``reserve`` submits the WorkItem (``SubmitWorkItem``) under a key-derived
  identity whose command digest is the request binding, then leases it
  (``ClaimWorkItem``). A fresh lease is the only acquisition. A replay with a
  different binding is EG's idempotency conflict. A terminal item replays its
  stored outcome; any other existing item is pending and never reacquired.
* ``complete`` stores the outcome on the leased item (``CasWorkItemMetadata``)
  and commits the terminal result under the lease fence
  (``CommitWorkItemResult``).

The item allows one attempt and is leased for a day, so EG never retries a
started effect. GraphOS keeps no local copy.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from graph_os.api.invoke.audit import EffectReservation
from graph_os.api.invoke.steps import OpError, OpResult

__all__ = ["EFFECT_KIND", "EgEffectJournal", "effect_work_item_id"]

#: The WorkItem kind of one governed effect.
EFFECT_KIND = "graphos.effect"
_LEASE_MS = 24 * 60 * 60 * 1000
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead_letter"})


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def effect_work_item_id(key: str) -> str:
    """The EG WorkItem id of the effect reserved under ``key``."""
    return f"graphos-effect-{_sha256(key)[:48]}"


def _encode_outcome(outcome: OpResult | OpError) -> dict[str, Any]:
    if isinstance(outcome, OpResult):
        return {
            "type": "result",
            "value": outcome.value,
            "code": outcome.code,
            "details": dict(outcome.details),
        }
    return {
        "type": "error",
        "code": outcome.code,
        "details": dict(outcome.details),
        "source": outcome.source,
    }


def _decode_outcome(stored: Any) -> OpResult | OpError | None:
    if not isinstance(stored, str):
        return None
    data = json.loads(stored)
    if data.get("type") == "result":
        return OpResult(data.get("value"), data.get("code", "OK"), data["details"])
    if data.get("type") == "error":
        return OpError(data["code"], data["details"], data.get("source", "graphos"))
    return None


def _request_context(
    claims: Mapping[str, Any], client: Any, key: str
) -> dict[str, Any]:
    """The WorkItem's typed request context, from the verified service claims."""
    now_ms = int(time.time() * 1000)
    graph = getattr(client, "graph_name", None) or getattr(client, "_graph_name", "")
    return {
        "schema_version": "2",
        "agent_id": str(claims["agent_id"]),
        "subject_id": str(claims["principal"]),
        "tenant_id": str(claims["tenant"]),
        "audience": str(claims["audience"]),
        "policy_version": str(claims["policy_version"]),
        "scopes": list(claims.get("scopes") or ()),
        "graph": str(graph),
        "authentication_method": "oidc",
        "issued_at_ms": now_ms,
        "expires_at_ms": now_ms + _LEASE_MS,
        "request_id": f"graphos-effect:{_sha256(key)[:32]}",
        "trace_id": _sha256(key)[:32],
        "placement_epoch": None,
    }


class EgEffectJournal:
    """``EffectJournal`` over EG WorkItems, under the GraphOS service authority."""

    def __init__(
        self,
        client: Callable[[], Awaitable[Any]],
        *,
        claims: Callable[[], Mapping[str, Any]],
    ) -> None:
        self._client = client
        self._claims = claims
        self._worker = f"graph-os-effects-{secrets.token_hex(8)}"

    async def _items(self) -> tuple[Any, Mapping[str, Any]]:
        client = await self._client()
        return client, self._claims()

    async def reserve(self, key: str, binding: str) -> EffectReservation:
        client, claims = await self._items()
        tenant = str(claims["tenant"])
        item_id = effect_work_item_id(key)
        with client.use_verified_context(dict(claims)):
            try:
                submitted = await self._submit(client, claims, item_id, key, binding)
            except Exception as exc:
                # A replayed submit is a new envelope for the same key: EG
                # refuses it as an idempotency conflict. The stored item, not
                # the refusal, decides the reservation.
                if "IDEMPOTENCY_CONFLICT" not in str(exc.args[0] if exc.args else ""):
                    raise
                return await self._existing(client, tenant, item_id, binding)
            if submitted.get("created"):
                return await self._acquire(client, tenant, item_id)
            return await self._existing(client, tenant, item_id, binding)

    async def _submit(
        self,
        client: Any,
        claims: Mapping[str, Any],
        item_id: str,
        key: str,
        binding: str,
    ) -> Any:
        return await client.work_items.submit(
            {
                "schema_version": "1",
                "context": _request_context(claims, client, key),
                "work_item_id": item_id,
                "idempotency_key": f"graphos-effect:{_sha256(key)}",
                "command_digest": _sha256(binding),
                "kind": EFFECT_KIND,
                "priority": 0,
                "depends_on": [],
                # The binding digest carries the operation, arguments,
                # caller authority and registry digest of the effect.
                "input_ref": f"graphos-effect-binding:{_sha256(binding)}",
                "policy_digest": _sha256(str(claims["policy_version"])),
                "catalog_digest": _sha256(binding),
                "model_digest": "none",
                "max_attempts": 1,
                "deadline_unix": None,
                "metadata": {"binding": binding},
                "provenance_refs": [],
                "max_tenant_in_flight": 4096,
            }
        )

    async def _acquire(self, client: Any, tenant: str, item_id: str) -> Any:
        answer = await client.work_items.claim(
            {
                "schema_version": "1",
                "tenant_ref": tenant,
                "work_item_id": item_id,
                "queue_ref": None,
                "resource_class": None,
                "fairness_group": None,
                "worker_ref": self._worker,
                "now_ms": int(time.time() * 1000),
                "lease_ms": _LEASE_MS,
                "max_tenant_in_flight": 4096,
            }
        )
        if not answer.get("claimed"):
            return EffectReservation(item_id, "pending")
        reference = json.dumps(
            {
                "tenant": tenant,
                "work_item_id": item_id,
                "worker_id": str(answer.get("lease_holder_ref", self._worker)),
                "lease_epoch": int(answer.get("lease_epoch", 0)),
                "fencing_token": int(answer.get("fencing_token", 0)),
            },
            sort_keys=True,
        )
        return EffectReservation(reference, "acquired")

    async def _existing(
        self, client: Any, tenant: str, item_id: str, binding: str
    ) -> EffectReservation:
        item = await client.work_items.get(tenant=tenant, work_item_id=item_id)
        metadata = (item or {}).get("metadata") or {}
        if not item or metadata.get("binding") != binding:
            return EffectReservation(item_id, "conflict")
        if str(item.get("status")) in _TERMINAL:
            outcome = _decode_outcome(metadata.get("outcome"))
            if outcome is not None:
                return EffectReservation(item_id, "completed", outcome)
        return EffectReservation(item_id, "pending")

    async def complete(self, reference: str, outcome: OpResult | OpError) -> None:
        fence = json.loads(reference)
        client, claims = await self._items()
        now_ms = int(time.time() * 1000)
        with client.use_verified_context(dict(claims)):
            item = await client.work_items.get(
                tenant=fence["tenant"], work_item_id=fence["work_item_id"]
            )
            metadata = dict((item or {}).get("metadata") or {})
            stored = await client.work_items.cas_metadata(
                tenant=fence["tenant"],
                work_item_id=fence["work_item_id"],
                expected_status=[str((item or {}).get("status"))],
                now_ms=now_ms,
                expected_lease={
                    "worker_ref": fence["worker_id"],
                    "lease_epoch": fence["lease_epoch"],
                    "fencing_token": fence["fencing_token"],
                },
                expected_metadata=metadata,
                set_metadata={
                    **metadata,
                    "outcome": json.dumps(_encode_outcome(outcome), sort_keys=True),
                },
            )
            if stored.get("outcome") != "applied":
                raise RuntimeError("EG did not store the effect outcome")
            terminal = "succeeded" if isinstance(outcome, OpResult) else "failed"
            committed = await client.work_items.commit_result(
                idempotency_key=f"graphos-effect-commit:{fence['work_item_id']}",
                outcome=terminal,
                now_ms=now_ms,
                **fence,
            )
            if str(committed.get("status")) != terminal:
                raise RuntimeError("EG did not commit the effect outcome")
