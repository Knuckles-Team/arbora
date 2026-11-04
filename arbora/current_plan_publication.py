"""Run-local publication using an admitted WorkItem's actual native claim."""

from __future__ import annotations

import asyncio
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
from arbora.security.anonymous import client_scope, session_context


@dataclass
class _Binding:
    engine: Any
    session: Any
    run_id: str
    work_item_id: str
    claim: Mapping[str, Any]
    hosted_invocation: Any
    owner_graph: str = CONTROL_GRAPH
    revision_offset: int | None = None
    publication_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    initialization_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    active: bool = True
    payload: dict[str, Any] | None = field(default=None, repr=False)

    def require_active(self) -> None:
        if not self.active:
            raise RuntimeError("current-plan publication binding is closed")
        session_context(self.session)
        if time.time_ns() // 1_000_000 >= self.claim["lease_expires_at_ms"]:
            raise RuntimeError("current-plan publication lease expired")

    async def publish(self, revision: int, items: tuple[Any, ...]) -> None:
        async with self.publication_lock:
            await self._publish(revision, items)

    async def _publish(self, revision: int, items: tuple[Any, ...]) -> None:
        self.require_active()
        if self.revision_offset is None:
            raise RuntimeError("current-plan publication was not initialized")
        revision += self.revision_offset
        now = time.time_ns() // 1_000_000
        expiry = self.claim["lease_expires_at_ms"]
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
            expected = (
                self.revision_offset
                if self.payload is None
                else self.payload["snapshot"]["revision"] + 1
            )
            if revision != expected:
                raise ValueError("current-plan revision conflict")
            identity = [self.owner_graph, self.session.tenant, self.run_id, revision]
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
        from medulla.generated.coordination import (
            decode_publish_current_plan,
            send_publish_current_plan,
        )

        client = self.engine.graph_compute.for_graph(self.owner_graph).async_client
        await _admit(self, "eg.coordination.PublishCurrentPlan")
        self.require_active()
        with _session_client_scope(client, self.session):
            response = await send_publish_current_plan(
                client,
                {"request": json.loads(json.dumps(self.payload))},
                self.owner_graph,
                idempotency_key=self.payload["idempotency_key"],
            )
        await _admit(self, "eg.coordination.PublishCurrentPlan")
        self.require_active()
        receipt = decode_publish_current_plan(response)
        if receipt.run_id != self.run_id:
            raise RuntimeError("current-plan publication receipt run mismatch")
        if receipt.revision != self.payload["snapshot"]["revision"]:
            raise RuntimeError("current-plan publication receipt revision mismatch")


_CURRENT: contextvars.ContextVar[_Binding | None] = contextvars.ContextVar(
    "arbora_current_plan_publication", default=None
)


def _validate_claim_identity(
    session: Any,
    work_item_id: str,
    claim: Mapping[str, Any],
) -> None:
    if claim.get("tenant") != session.tenant:
        raise ValueError("invalid admitted run claim")
    if claim.get("claimed") is not True:
        raise ValueError("invalid admitted run claim")
    if claim.get("work_item_id") != work_item_id:
        raise ValueError("claim does not name the admitted WorkItem")


def _validate_claim_holder(claim: Mapping[str, Any]) -> None:
    holder = claim.get("lease_holder_ref")
    if not isinstance(holder, str):
        raise ValueError("claim has no native holder")
    if not holder.strip():
        raise ValueError("claim has no native holder")


def _validate_claim_fence(claim: Mapping[str, Any]) -> None:
    for key in ("lease_epoch", "fencing_token", "lease_expires_at_ms"):
        value = claim.get(key)
        if type(value) is not int:
            raise ValueError("claim has no native fence or expiry")
        if value <= 0:
            raise ValueError("claim has no native fence or expiry")


def _validate_binding(
    *,
    session: Any,
    work_item_id: str,
    claim: Mapping[str, Any],
    hosted_invocation: Any,
    owner_graph: str,
) -> None:
    if hosted_invocation is None:
        raise ValueError("trusted publication host is required")
    session_context(session)
    if not owner_graph:
        raise ValueError("invalid native owner graph")
    if owner_graph != owner_graph.strip():
        raise ValueError("invalid native owner graph")
    _validate_claim_identity(session, work_item_id, claim)
    _validate_claim_holder(claim)
    _validate_claim_fence(claim)


@contextlib.contextmanager
def bind_admitted_claim(
    engine: Any,
    *,
    session: Any,
    run_id: str,
    work_item_id: str,
    claim: Mapping[str, Any],
    hosted_invocation: Any,
    owner_graph: str = CONTROL_GRAPH,
) -> Iterator[None]:
    """Bind only from the native admission/claim call path, never request metadata.

    Closing stops new dispatch, including from copied task contexts. It cannot
    retract an in-flight native write; EG remains the lease/owner authority.
    """
    _validate_binding(
        session=session,
        work_item_id=work_item_id,
        claim=claim,
        hosted_invocation=hosted_invocation,
        owner_graph=owner_graph,
    )
    binding = _Binding(
        engine,
        session,
        run_id,
        work_item_id,
        dict(claim),
        hosted_invocation,
        owner_graph,
    )
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
    async with binding.initialization_lock:
        if binding.revision_offset is None:
            await _initialize(binding)
    yield binding


async def _initialize(binding: _Binding) -> None:
    """Pin the next native revision once; concurrent writes must fail natively.

    EG atomically requires an increasing revision under the exact live fence.
    We advance by exactly one, never silently reread/rebase a failed write.
    Scaffold-only updates retain the executable attachment in native storage.
    """
    from medulla.generated.coordination import (
        decode_get_current_plan,
        send_get_current_plan,
    )

    client = binding.engine.graph_compute.for_graph(binding.owner_graph).async_client
    await _admit(binding, "eg.coordination.GetCurrentPlan")
    binding.require_active()
    with _session_client_scope(client, binding.session):
        previous = decode_get_current_plan(
            await send_get_current_plan(
                client,
                {"run_id": binding.run_id, "tenant": binding.session.tenant},
                binding.owner_graph,
            )
        )
    await _admit(binding, "eg.coordination.GetCurrentPlan")
    binding.require_active()
    if previous is None:
        binding.revision_offset = 0
        return
    snapshot = previous.model_dump(mode="json")
    if (
        snapshot["run_id"] != binding.run_id
        or type(snapshot["revision"]) is not int
        or snapshot["revision"] < 0
    ):
        raise ValueError("invalid current-plan baseline")
    binding.revision_offset = snapshot["revision"] + 1


async def _admit(binding: _Binding, operation: str) -> None:
    """Native ownership does not replace the host operation policy gate."""
    from arbora.api.mcp.caller import caller_from_session
    from arbora.mcp_server.task_plan_runtime import _admit as admit_operation

    binding.require_active()
    caller = caller_from_session(binding.session, credential_kind="session")
    await admit_operation(binding.hosted_invocation, caller, operation)
    binding.require_active()


@contextlib.contextmanager
def _session_client_scope(client: Any, session: Any) -> Iterator[None]:
    """Bind routed views at dispatch; bind raw transports through their carrier.

    SessionRoutedClient is not a transport: its _send resolves the exact ambient
    session and applies client_scope to the real pooled client for each request.
    Treating its deferred attributes as raw-client flags or context managers is
    invalid. Other clients still require the explicit verified/anonymous carrier.
    """
    from arbora.mcp_server.served_engine import SessionRoutedClient

    with use_session(session):
        if isinstance(client, SessionRoutedClient):
            yield
        else:
            with client_scope(client, session_context(session)):
                yield
