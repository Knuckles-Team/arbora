"""Trusted run-local carrier shared by host publication and checkpoint adapters."""

from __future__ import annotations

import contextlib
import contextvars
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AdmittedClaim:
    engine: Any
    session: Any
    run_id: str
    work_item_id: str
    claim: Mapping[str, Any]
    active: bool = True
    adapters: dict[str, Any] = field(default_factory=dict, repr=False)

    def require_active(self) -> None:
        if not self.active:
            raise RuntimeError("admitted run binding is closed")
        self.session.engine_verified_context()
        if time.time_ns() // 1_000_000 >= self.claim["lease_expires_at_ms"]:
            raise RuntimeError("admitted run lease expired")


_CURRENT: contextvars.ContextVar[AdmittedClaim | None] = contextvars.ContextVar(
    "arbora_admitted_run", default=None
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
    binding = AdmittedClaim(engine, session, run_id, work_item_id, dict(claim))
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        binding.active = False
        _CURRENT.reset(token)


def current_admitted_claim(request: Any) -> AdmittedClaim | None:
    """Select only the host-bound claim; request metadata never grants authority."""
    binding = _CURRENT.get()
    if binding is None:
        return None
    if request.run_id != binding.run_id:
        raise RuntimeError("native run admission authority is unavailable")
    if request.metadata.get("session") is not binding.session:
        raise RuntimeError("native run admission session differs")
    binding.require_active()
    return binding
