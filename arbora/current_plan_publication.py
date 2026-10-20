"""Run-local publication using an admitted WorkItem's actual native claim."""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from arbora.admitted_run import AdmittedClaim, current_admitted_claim
from arbora.api.contracts.session import use_session
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH


@dataclass
class _Publisher:
    binding: AdmittedClaim
    payload: dict[str, Any] | None = field(default=None, repr=False)

    async def publish(self, revision: int, items: tuple[Any, ...]) -> None:
        self.binding.require_active()
        now = time.time_ns() // 1_000_000
        expiry = self.binding.claim["lease_expires_at_ms"]
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
            identity = [
                CONTROL_GRAPH,
                self.binding.session.tenant,
                self.binding.run_id,
                revision,
            ]
            self.payload = {
                "tenant": self.binding.session.tenant,
                "snapshot": {
                    "run_id": self.binding.run_id,
                    "revision": revision,
                    "observed_at_ms": now,
                    "expires_at_ms": expiry,
                    "items": rendered,
                },
                "worker_id": self.binding.claim["lease_holder_ref"],
                "lease_epoch": self.binding.claim["lease_epoch"],
                "fencing_token": self.binding.claim["fencing_token"],
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

        client = self.binding.engine.graph_compute.for_graph(CONTROL_GRAPH).async_client
        self.binding.require_active()
        with use_session(self.binding.session):
            response = await send_publish_current_plan(
                client,
                {"request": json.loads(json.dumps(self.payload))},
                CONTROL_GRAPH,
                idempotency_key=self.payload["idempotency_key"],
            )
        decode_publish_current_plan(response)


@contextlib.asynccontextmanager
async def publication_scope(request: Any) -> AsyncIterator[_Publisher | None]:
    """Select a publication port from the host's admitted run carrier."""
    binding = current_admitted_claim(request)
    if binding is None:
        yield None
        return
    # Keep uncertain publication requests stable across scopes in one admission.
    if "current_plan" not in binding.adapters:
        binding.adapters["current_plan"] = _Publisher(binding)
    yield binding.adapters["current_plan"]
