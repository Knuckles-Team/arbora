"""The signed-ingress admission port of the gateway identity chokepoint (ARBORA-OPS-R896.8).

A signed webhook is one more way the identity middleware admits a request, beside the
bearer: it is not an exemption from admission. The middleware reads the raw body, hands
the path, headers and body to the bound admitter, and serves the request under the
service principal the admitter returns. An admitter that refuses, or none bound, leaves
the request unauthenticated: the caller sees the one uniform 401 and the audit event
carries the precise code. This module holds only the port; the verifier is composed by
the identity layer (``arbora.identity.signed_ingress``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

#: The one signed-ingress route family this admission method serves.
SCM_INGRESS_PREFIX = "/api/v1/development/scm-events/"
#: The scope key the middleware sets (never read from a request) on an admitted request.
ADMITTED_STATE_KEY = "arbora_signed_ingress"
#: The credential kind of a caller admitted by a signed delivery.
SIGNED_CREDENTIAL_KIND = "signed_ingress"


class SignedIngressRefused(PermissionError):
    """The signed delivery was refused; ``code`` is for the audit event, never the body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AdmittedIngress:
    """The verified facts of one admitted delivery: the source, its principal and the delivery."""

    source_id: str
    principal: str
    tenant: str
    delivery_id: str
    scopes: tuple[str, ...]
    expires_at: int
    """Epoch seconds the admission lasts: one request, never longer than the skew window."""


class SignedIngressAdmitter(Protocol):
    """Verify one delivery: signature, skew and replay; raise ``SignedIngressRefused``."""

    async def admit(
        self, path: str, headers: Mapping[str, str], body: bytes
    ) -> AdmittedIngress: ...


_BOUND: list[SignedIngressAdmitter | None] = [None]


def bind_signed_ingress(admitter: SignedIngressAdmitter | None) -> None:
    """Bind (or clear) the admitter at the host composition root."""
    _BOUND[0] = admitter


def bound_signed_ingress() -> SignedIngressAdmitter | None:
    """The admitter the composition root bound, or ``None``."""
    return _BOUND[0]


def is_signed_ingress_path(path: Any) -> bool:
    """True for a path the signed-ingress admission method serves."""
    return isinstance(path, str) and path.startswith(SCM_INGRESS_PREFIX)
