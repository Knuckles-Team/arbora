"""Project native-issued managed-start grants without minting authority.

The configured native principal fingerprint is supplied by the access
composition owner, never by launch parameters. Native provenance is a top-level storage-owned field; an
identically named value inside generic grant JSON is not evidence.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from arbora.agent_host.start_admission import HostStartGrant, HostStartRequest
from arbora.identity.ports import SessionAuthority, SessionState


class NativeHostStartGrants:
    """Read an exact existing lease using a caller-bound public native client."""

    def __init__(self, client: Any, *, lease_id: str, issuer_ref: str) -> None:
        if any(
            type(value) is not str or not value.strip()
            for value in (lease_id, issuer_ref)
        ):
            raise ValueError("host_start_owner_required")
        self._client = client
        self._lease_id = lease_id
        self._issuer_ref = issuer_ref

    async def authorize(
        self, state: SessionState, request: HostStartRequest
    ) -> HostStartGrant | None:
        claims = state.resolution.request_context
        lease = await self._client.control_leases.get(
            tenant=claims["tenant"], lease_id=self._lease_id
        )
        if not isinstance(lease, dict):
            return None
        provenance = lease.get("host_start_provenance")
        if (
            lease.get("lease_id") != self._lease_id
            or lease.get("kind") != "host.start"
            or lease.get("status") != "active"
            or not isinstance(provenance, dict)
            or set(provenance) != {"schema_version", "issuer_ref"}
            or type(provenance.get("schema_version")) is not int
            or provenance["schema_version"] != 1
            or provenance.get("issuer_ref") != self._issuer_ref
        ):
            return None
        grant = lease.get("grant")
        if not isinstance(grant, dict):
            return None
        expected = {
            "principal_ref": state.resolution.principal_id,
            "session_ref": state.session_ref,
            "workspace_ref": request.workspace,
            "configuration_digest": request.configuration_digest,
            "interfaces": request.interfaces,
            "web_logs": request.web_logs,
            "policy_revision": claims["policy_version"],
        }
        if set(grant) != {*expected, "session_expires_at_ms"} or any(
            type(grant[key]) is not type(value) or grant[key] != value
            for key, value in expected.items()
        ):
            return None
        revision = lease.get("revision")
        expiry = lease.get("expires_at_ms")
        hard_expiry = lease.get("hard_expires_at_ms")
        session_expiry = grant["session_expires_at_ms"]
        if (
            type(revision) is not int
            or type(expiry) is not int
            or type(hard_expiry) is not int
            or type(session_expiry) is not int
            or revision < 1
            or not int(time.time() * 1000) < expiry <= hard_expiry <= session_expiry
            or session_expiry > state.expires_at_ms
        ):
            return None
        return HostStartGrant(
            request=request,
            tenant=claims["tenant"],
            principal=state.resolution.principal_id,
            session_ref=state.session_ref,
            policy_revision=claims["policy_version"],
            revision=json.dumps([self._lease_id, revision], separators=(",", ":")),
            expires_at_ms=expiry,
            action="host.start",
        )


class HostStartIssueUnconfirmed(RuntimeError):
    """Issuance may have committed; callers must reconcile, never auto-reissue."""

    def __init__(self, lease_id: str) -> None:
        super().__init__("host_start_issue_requires_reconciliation")
        self.lease_id = lease_id


class NativeHostStartIssuer:
    """Access-owner composition over the dedicated native issuance operation.

    Supply an already authenticated issuer client, authorized by the native
    ``host:issue-start`` permission, and the existing session resolver. This
    class is not a public operation accepting caller identity in parameters and
    does not register/grant privileges or replace the governed fleet launcher.
    The owner chooses issuance under its existing policy. Missing native support
    refuses; generic control-leases issuance is never a fallback.
    """

    def __init__(
        self, client: Any, sessions: SessionAuthority, *, issuer_ref: str
    ) -> None:
        self._client = client
        self._sessions = sessions
        self._issuer_ref = issuer_ref

    async def issue(
        self,
        credential: str,
        request: HostStartRequest,
        *,
        lease_id: str,
        expires_at_ms: int,
        idempotency_key: str,
    ) -> HostStartGrant:
        from arbora.agent_host.start_admission import _same_session

        reader = NativeHostStartGrants(
            self._client, lease_id=lease_id, issuer_ref=self._issuer_ref
        )
        state = await self._sessions.resolve_session(credential)
        if (
            not isinstance(state, SessionState)
            or type(expires_at_ms) is not int
            or not int(time.time() * 1000) < expires_at_ms <= state.expires_at_ms
            or type(idempotency_key) is not str
            or not idempotency_key.strip()
        ):
            raise PermissionError("workspace_denied")
        issue = getattr(self._client.control_leases, "issue_host_start", None)
        if not callable(issue):
            raise RuntimeError("host_start_native_producer_unavailable")
        claims = state.resolution.request_context
        binding = {
            "principal_ref": state.resolution.principal_id,
            "session_ref": state.session_ref,
            "workspace_ref": request.workspace,
            "configuration_digest": request.configuration_digest,
            "interfaces": request.interfaces,
            "web_logs": request.web_logs,
            "policy_revision": claims["policy_version"],
            "session_expires_at_ms": state.expires_at_ms,
        }
        # Once dispatch begins an exception is not proof that no grant exists.
        try:
            result = await issue(
                tenant=claims["tenant"],
                lease_id=lease_id,
                binding=binding,
                expires_at_ms=expires_at_ms,
                idempotency_key=idempotency_key,
            )
            if not isinstance(result, dict) or result.get("outcome") not in {
                "issued",
                "collision",
            }:
                raise RuntimeError("host_start_native_result_unavailable")
            refreshed = await self._sessions.resolve_session(credential)
            if not _same_session(state, refreshed):
                raise PermissionError("workspace_denied")
            grant = await reader.authorize(refreshed, request)
            final = await self._sessions.resolve_session(credential)
            if (
                not _same_session(refreshed, final)
                or grant is None
                or grant.expires_at_ms > expires_at_ms
            ):
                raise PermissionError("workspace_denied")
        except asyncio.CancelledError as exc:
            # Preserve task cancellation while retaining reconciliation identity.
            # Cancellation after dispatch is not evidence of non-issuance.
            exc.add_note(f"host_start_issue_requires_reconciliation: {lease_id}")
            raise
        except Exception as exc:
            raise HostStartIssueUnconfirmed(lease_id) from exc
        return grant
