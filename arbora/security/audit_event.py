"""The privacy-safe audit payload of one operation call."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from arbora.core.claims import params_digest

__all__ = ["AuditSubject", "audit_event"]


class AuditSubject(Protocol):
    """The three caller facts an audit event records."""

    @property
    def principal(self) -> str: ...

    @property
    def tenant(self) -> str: ...

    @property
    def request_id(self) -> str: ...


def audit_event(
    op: Any,
    params: Mapping[str, Any],
    caller: AuditSubject,
    surface: Any,
    status: str,
    audit_ref: str = "",
    evidence: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Record identity and a digest, never argument values.

    ``evidence`` holds identifiers and counts only (screening fields and the
    operator hook decisions); it never holds matched or redacted text.
    """

    event = {
        "op": op.id,
        "surface": surface.value,
        "principal": caller.principal,
        "tenant": caller.tenant,
        "plan_digest": params_digest(params),
        "result_status": status,
        "request_id": caller.request_id,
    }
    if audit_ref:
        event["audit_ref"] = audit_ref
    if evidence:
        event.update(evidence)
    if op.id == "fleet.call":
        server, tool = params.get("server"), params.get("tool")
        if isinstance(server, str) and isinstance(tool, str):
            event["target"] = f"{server}/{tool}"
    if getattr(op.executor, "value", None) == "service":
        event["owner"] = caller.principal
    return event
