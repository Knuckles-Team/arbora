"""The child call path asks the credential authority first (ARBORA-OPS-R893).

A child whose catalog entry declares a ``credential_grant`` (``{resource, protocol, scopes}`` plus the connector's provider, audience, vault reference and token profile) is
called only under the binding of the exact current grant. A host with no authority bound,
or a refusal from it, answers the call as an error before anything is sent to the child.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from arbora.fleet.credential_authority import CredentialAuthority, CredentialDenied
from arbora.pin_lag.unavailable import PinLagUnavailable


class CredentialRefusal(Exception):
    """The call is not admitted; ``code`` is the closed reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _caller() -> tuple[str, str, str]:
    from arbora.security.session import current_session

    session = current_session()
    if session is None or not getattr(session.actor, "authenticated", False):
        raise CredentialRefusal("binding_mismatch")
    principal = str(session.actor.actor_id)
    client = str(getattr(session.actor, "client_id", "") or principal)
    return str(session.tenant), principal, client


@contextlib.asynccontextmanager
async def credentialed_call(
    authority: CredentialAuthority | None,
    server_name: str,
    original_name: str,
    cfg: Mapping[str, Any],
) -> AsyncIterator[None]:
    """Bind the verified grant for one child call, or raise :class:`CredentialRefusal`."""
    spec = cfg.get("credential_grant")
    if not spec:
        yield
        return
    if authority is None:
        raise CredentialRefusal("authority_unavailable")
    tenant, principal, client = _caller()
    try:
        async with authority.admitted(
            tenant=tenant,
            requesting_principal=principal,
            client=client,
            connector=server_name,
            operation=original_name,
            resource=str(spec["resource"]),
            protocol=str(spec["protocol"]),
            profile=spec,
            scopes=tuple(spec.get("scopes") or ()),
        ):
            yield
    except CredentialDenied as denied:
        raise CredentialRefusal(denied.reason) from None
    except PinLagUnavailable:
        raise CredentialRefusal("authority_unavailable") from None
