"""The approval reference a connector call carries (ARBORA-OPS-R009.5).

One helper at the one chokepoint: every child call path (the dedicated
service-credential session, the OAuth-gated session, and the fleet gateway's
delegated and service calls) builds its ``tools/call`` ``_meta`` here, so the
key and its shape exist once.
"""

from __future__ import annotations

__all__ = ["APPROVAL_META_KEY", "approval_call_kwargs", "approval_kwargs"]

#: The ``_meta`` key a connector reads the approval reference from.
APPROVAL_META_KEY = "arbora/approval_ref"


def approval_call_kwargs(approval_ref: str) -> dict[str, dict[str, str]]:
    """The ``meta`` keyword of one connector ``tools/call``; empty without approval."""
    return {"meta": {APPROVAL_META_KEY: approval_ref}} if approval_ref else {}


def approval_kwargs(approval_ref: str) -> dict[str, str]:
    """Keyword arguments a transport receives; empty when no approval exists."""
    return {"approval_ref": approval_ref} if approval_ref else {}
