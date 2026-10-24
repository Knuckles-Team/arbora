"""Existing refusal boundaries for the proposed local attended contract.

These checks do not implement or qualify local browser admission.
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbora.api.invoke.pipeline import _base_authority
from arbora.api.invoke.steps import AnonymousCaller, authenticate
from arbora.api.mcp.caller import caller_from_session
from arbora.api.registry import Effect, PrincipalRule
from arbora.security import anonymous
from tests.browser_control.test_browser_control_service import _binding
from tests.browser_control.test_browser_control_service import session as session
from tests.security.test_anonymous_mode import local_mode as local_mode

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


def test_local_provenance_cannot_be_promoted_to_authenticated(local_mode):
    caller = caller_from_session(anonymous.session(), credential_kind="none")
    assert isinstance(caller, AnonymousCaller)
    assert caller.authenticated is False
    assert caller.mfa_at_ms is None
    assert authenticate(caller) is None
    assert authenticate(replace(caller, authenticated=True)) is not None


def test_raw_anonymous_transport_does_not_admit_browser_origin(local_mode):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/browser-control/attended-arm",
        "client": ("127.0.0.1", 1000),
        "headers": [
            (b"host", b"127.0.0.1:38001"),
            (b"origin", b"http://127.0.0.1:38001"),
        ],
    }
    with pytest.raises(PermissionError, match="Origin"):
        anonymous.validate_request(scope)


@pytest.mark.parametrize(
    "change",
    [{"attended_issuer": ""}, {"attended_acr": ""}, {"attended_auth_time": 0.0}],
)
def test_existing_oidc_binding_cannot_use_empty_local_placeholders(session, change):
    with pytest.raises(ValueError):
        _binding(session, **change)


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", [Effect.READ, Effect.WRITE])
async def test_anonymous_reads_and_writes_retain_canonical_policy(local_mode, effect):
    caller = caller_from_session(anonymous.session(), credential_kind="none")
    operation = SimpleNamespace(
        id="browser.request_call",
        principals=PrincipalRule.ANY,
        scopes=frozenset(),
        effect=effect,
    )
    policy = SimpleNamespace(check_op=AsyncMock(return_value=False))
    result = await _base_authority(
        operation, caller, SimpleNamespace(policy_gate=policy)
    )
    assert result is not None and result.code == "POLICY_DENIED"
    policy.check_op.assert_awaited_once_with(operation, caller)
