"""Run-local publication using an admitted WorkItem's actual native claim."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from arbora.api.contracts.session import use_session
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH


@dataclass
class _Binding:
    engine: Any
    session: Any
    run_id: str
    work_item_id: str
    claim: Mapping[str, Any]
    active: bool = True
    payload: dict[str, Any] | None = field(default=None, repr=False)

    def require_active(self) -> None:
        if not self.active:
            raise RuntimeError("current-plan publication binding is closed")
        self.session.engine_verified_context()

    async def publish(self, revision: int, items: tuple[Any, ...]) -> None:
        self.require_active()
        now = time.time_ns() // 1_000_000
        expiry = self.claim["lease_expires_at_ms"]
        if now >= expiry:
            raise RuntimeError("current-plan publication lease expired")
        rendered = [item.model_dump(mode="json") for item in items]
        if (
            self.payload is not None
            and revision <= self.payload["snapshot"]["revision"]
        ):
            if (revision, rendered) != (
                self.payload["snapshot"]["revision"],
                self.payload["snapshot"]["items"],
            ):
                raise ValueError("current-plan revision conflict")
        else:
            identity = [CONTROL_GRAPH, self.session.tenant, self.run_id, revision]
            self.payload = {
                "tenant": self.session.tenant,
                "snapshot": {
                    "run_id": self.run_id,
                    "revision": revision,
                    "observed_at_ms": now,
                    "expires_at_ms": expiry,
                    "items": rendered,
                },
                "worker_id": self.claim["lease_holder_ref"],
                "lease_epoch": self.claim["lease_epoch"],
                "fencing_token": self.claim["fencing_token"],
                "idempotency_key": hashlib.sha256(
                    json.dumps(identity).encode()
                ).hexdigest(),
            }
        # Retain the exact request before dispatch: an uncertain write is not retried
        # with a new observation time or extended expiry.
        from epistemic_graph.generated.coordination import (
            decode_publish_current_plan,
            send_publish_current_plan,
        )

        client = self.engine.graph_compute.for_graph(CONTROL_GRAPH).async_client
        self.require_active()
        with use_session(self.session):
            response = await send_publish_current_plan(
                client, {"request": json.loads(json.dumps(self.payload))}, CONTROL_GRAPH
            )
        decode_publish_current_plan(response)


_CURRENT: contextvars.ContextVar[_Binding | None] = contextvars.ContextVar(
    "arbora_current_plan_publication", default=None
)


@contextlib.contextmanager
def bind_admitted_claim(
    engine: Any,
    *,
    session: Any,
    run_id: str,
    work_item_id: str,
    claim: Mapping[str, Any],
) -> Iterator[None]:
    """Bind only from the native admission/claim call path, never request metadata.

    Closing stops new dispatch, including from copied task contexts. It cannot
    retract an in-flight native write; EG remains the lease/owner authority.
    """
    session.engine_verified_context()
    if claim.get("tenant") != session.tenant or claim.get("claimed") is not True:
        raise ValueError("invalid admitted run claim")
    if claim.get("work_item_id") != work_item_id:
        raise ValueError("claim does not name the admitted WorkItem")
    if (
        not isinstance(claim.get("lease_holder_ref"), str)
        or not claim["lease_holder_ref"].strip()
    ):
        raise ValueError("claim has no native holder")
    for key in ("lease_epoch", "fencing_token", "lease_expires_at_ms"):
        if type(claim.get(key)) is not int or claim[key] <= 0:
            raise ValueError("claim has no native fence or expiry")
    binding = _Binding(engine, session, run_id, work_item_id, dict(claim))
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        binding.active = False
        _CURRENT.reset(token)


@contextlib.asynccontextmanager
async def publication_scope(request: Any) -> AsyncIterator[_Binding | None]:
    """The AU factory selects a host binding, not an authority in AU metadata."""
    binding = _CURRENT.get()
    if binding is None:
        yield None
        return
    if request.run_id != binding.run_id:
        raise RuntimeError("native run publication authority is unavailable")
    if request.metadata.get("session") is not binding.session:
        raise RuntimeError("native run publication session differs")
    binding.require_active()
    yield binding
