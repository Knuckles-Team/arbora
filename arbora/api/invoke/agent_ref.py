"""Retrieval operations carry the verified agent reference (ARBORA-OPS-R883).

One shared step of the invoke pipeline, applied to the six bound retrieval methods. For
an agent caller the verified agent identity overwrites ``agent_ref``; a value in the
params that differs refuses ``AGENT_REF_FORGED``. A human caller sends none, so a
supplied value is a forgery too. No handler of the six operations sets the field itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.invoke.steps import OpError
from arbora.core.claims import freeze_claims

AGENT_REF_METHODS = frozenset(
    {
        "Discover",
        "ContextCompile",
        "CorpusRetrieve",
        "CodeContext",
        "Diagnose",
        "RetrievalFeedbackRecord",
    }
)


def _method(op: Any) -> str | None:
    binding = getattr(op, "binding", None)
    if getattr(binding, "kind", None) != "eg":
        return None
    service = getattr(binding, "service", None)
    return service if service in AGENT_REF_METHODS else None


def bind_agent_ref(
    op: Any, arguments: Mapping[str, Any], caller: Any
) -> Mapping[str, Any] | OpError:
    """The arguments with ``agent_ref`` set from the verified caller, or a refusal."""
    if _method(op) is None:
        return arguments
    supplied = arguments.get("agent_ref")
    verified = None
    if getattr(caller, "principal_kind", None) == "agent":
        claim = (getattr(caller, "engine_claims", None) or {}).get("agent_id")
        verified = str(claim) if claim else None
    if supplied is not None and supplied != verified:
        return OpError("AGENT_REF_FORGED")
    if verified is None:
        return arguments
    return freeze_claims({**arguments, "agent_ref": verified})


__all__ = ["AGENT_REF_METHODS", "bind_agent_ref"]
