"""Native grant projection through real admission; no deployed authority."""
# spec: ARBORA-HOST-R010.7.1

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbora.agent_host.start_admission import HostStartAdmission
from tests.agent_host.test_start_admission import Owners


def fixture(tmp_path: Path):
    owner = Owners(tmp_path)
    request, state = owner.request, owner.state
    view = {
        "lease_id": "start-one",
        "kind": "host.start",
        "status": "active",
        "host_start_provenance": {"schema_version": 1, "issuer_ref": "host-issuer"},
        "grant": {
            "principal_ref": "alice",
            "session_ref": state.session_ref,
            "workspace_ref": request.workspace,
            "configuration_digest": request.configuration_digest,
            "interfaces": "terminal",
            "web_logs": False,
            "policy_revision": "policy-1",
            "session_expires_at_ms": state.expires_at_ms,
        },
        "issued_at_ms": state.expires_at_ms - 60000,
        "expires_at_ms": state.expires_at_ms,
        "hard_expires_at_ms": state.expires_at_ms,
        "revision": 1,
    }
    read = AsyncMock(return_value=view)
    admission = HostStartAdmission.from_native_grant(
        owner,
        owner,
        SimpleNamespace(control_leases=SimpleNamespace(get=read)),
        lease_id="start-one",
        issuer_ref="host-issuer",
    )
    return owner, view, read, admission


def admit(owner, admission):
    return asyncio.run(admission.admit("exact-credential", owner.request, owner.graph))


def test_native_grant_reaches_existing_admission_with_lease_identity(tmp_path: Path):
    owner, _, read, admission = fixture(tmp_path)
    checked = admit(owner, admission)
    assert checked.graph_session is owner.graph
    assert checked.grant.revision == '["start-one",1]'
    read.assert_awaited_once_with(tenant="tenant", lease_id="start-one")


@pytest.mark.parametrize(
    "path,value",
    [
        (("kind",), "action_approval"),
        (("status",), "revoked"),
        (("lease_id",), "start-two"),
        (("host_start_provenance",), None),
        (("host_start_provenance", "issuer_ref"), "forged-issuer"),
        (("host_start_provenance", "schema_version"), True),
        (("grant", "principal_ref"), "mallory"),
        (("grant", "session_ref"), "other-session"),
        (("grant", "workspace_ref"), "/other-workspace"),
        (("grant", "configuration_digest"), "0" * 64),
        (("grant", "interfaces"), "both"),
        (("grant", "web_logs"), 0),
        (("grant", "policy_revision"), "stale"),
        (("expires_at_ms",), 0),
        (("revision",), True),
    ],
)
def test_foreign_stale_or_unqualified_native_view_refuses(tmp_path: Path, path, value):
    owner, view, _, admission = fixture(tmp_path)
    cursor = view
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    with pytest.raises(PermissionError):
        admit(owner, admission)


def test_generic_grant_json_cannot_supply_native_provenance(tmp_path: Path):
    owner, view, _, admission = fixture(tmp_path)
    view["grant"]["host_start_provenance"] = view.pop("host_start_provenance")
    with pytest.raises(PermissionError):
        admit(owner, admission)


def test_session_revocation_during_native_read_refuses(tmp_path: Path):
    owner, view, read, admission = fixture(tmp_path)

    async def revoke(**kwargs):
        owner.revoke = True
        return deepcopy(view)

    read.side_effect = revoke
    with pytest.raises(PermissionError):
        admit(owner, admission)


def test_read_denial_is_not_retried_or_downgraded(tmp_path: Path):
    owner, _, read, admission = fixture(tmp_path)
    read.side_effect = PermissionError("native refusal")
    with pytest.raises(PermissionError, match="native refusal"):
        admit(owner, admission)
    assert read.await_count == 1


def issuer_fixture(tmp_path: Path):
    owner, view, read, _ = fixture(tmp_path)
    issue = AsyncMock(return_value={"outcome": "issued", "lease": view})
    client = SimpleNamespace(
        control_leases=SimpleNamespace(get=read, issue_host_start=issue)
    )
    return owner, view, read, issue, client


def issue_and_admit(owner, client):
    async def run():
        admission = await HostStartAdmission.issue_native_grant(
            owner,
            owner,
            client,
            "exact-credential",
            owner.request,
            lease_id="start-one",
            issuer_ref="host-issuer",
            expires_at_ms=owner.state.expires_at_ms,
            idempotency_key="issue-once",
        )
        return await admission.admit("exact-credential", owner.request, owner.graph)

    return asyncio.run(run())


def test_access_owner_issues_only_dedicated_native_method(tmp_path: Path):
    owner, _, read, issue, client = issuer_fixture(tmp_path)
    checked = issue_and_admit(owner, client)
    assert checked.grant.action == "host.start"
    assert issue.await_count == 1 and read.await_count == 2
    kwargs = issue.call_args.kwargs
    assert kwargs["tenant"] == "tenant"
    assert kwargs["binding"]["session_ref"] == owner.state.session_ref
    assert kwargs["binding"]["principal_ref"] == "alice"
    assert (
        kwargs["binding"]["configuration_digest"] == owner.request.configuration_digest
    )
    assert "issuer_ref" not in kwargs and "credential" not in kwargs


def test_missing_native_issue_cannot_fall_back_to_generic_lease(tmp_path: Path):
    owner, _, _, _, client = issuer_fixture(tmp_path)
    del client.control_leases.issue_host_start
    client.control_leases.issue = AsyncMock()
    with pytest.raises(RuntimeError, match="native_producer_unavailable"):
        issue_and_admit(owner, client)
    client.control_leases.issue.assert_not_called()


@pytest.mark.parametrize("failure", ["disconnect", "revoked", "bad-result"])
def test_uncertain_issue_never_retries_or_claims_admission(tmp_path: Path, failure):
    from arbora.access.host_start import HostStartIssueUnconfirmed

    owner, _, _, issue, client = issuer_fixture(tmp_path)

    async def interrupted(**kwargs):
        if failure == "disconnect":
            raise TimeoutError("connection lost after dispatch")
        if failure == "revoked":
            owner.revoke = True
        return {"outcome": "issued" if failure == "revoked" else "unknown"}

    issue.side_effect = interrupted
    with pytest.raises(HostStartIssueUnconfirmed) as error:
        issue_and_admit(owner, client)
    assert error.value.lease_id == "start-one"
    assert issue.await_count == 1


def test_expired_session_prevents_issue(tmp_path: Path):
    owner, _, read, issue, client = issuer_fixture(tmp_path)
    owner.revoke = True
    with pytest.raises(PermissionError):
        issue_and_admit(owner, client)
    issue.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize(
    "field", ["expires_at_ms", "hard_expires_at_ms", "session_expires_at_ms"]
)
def test_native_grant_cannot_outlive_qualified_session(tmp_path: Path, field):
    owner, view, _, admission = fixture(tmp_path)
    if field == "session_expires_at_ms":
        view["grant"][field] += 1
    else:
        view[field] += 1
    with pytest.raises(PermissionError):
        admit(owner, admission)


def test_cancelled_issue_preserves_cancellation_and_reconciliation_id(tmp_path: Path):
    owner, _, read, issue, client = issuer_fixture(tmp_path)
    issue.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as error:
        issue_and_admit(owner, client)
    assert error.value.__notes__ == [
        "host_start_issue_requires_reconciliation: start-one"
    ]
    assert issue.await_count == 1
    read.assert_not_called()
