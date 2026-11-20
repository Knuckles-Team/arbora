"""Explicit no-auth composes the declared web UI read routes."""

import pytest

from arbora.identity.ports import IdentityUnavailable
from arbora.webui_host.session_boundary import identity_factory_kwargs


def _factory_with_policy(agent, *, auth_mode=None, anonymous_read_routes=frozenset()):
    pass


_DECLARED = frozenset({"/api/apps", "/api/apps/city/status"})


@pytest.mark.spec("ARBORA-IDENTITY-R004.7.3")
def test_none_mode_serves_shell_identity_and_app_reads():
    values = identity_factory_kwargs(
        "none",
        None,
        factory=_factory_with_policy,
        listener_host="127.0.0.1",
        read_routes=lambda: _DECLARED,
    )
    assert values == {"auth_mode": "none", "anonymous_read_routes": _DECLARED}
    assert (
        identity_factory_kwargs(
            "none",
            None,
            factory=_factory_with_policy,
            listener_host="localhost",
            read_routes=lambda: _DECLARED,
        )["anonymous_read_routes"]
        == _DECLARED
    )


@pytest.mark.spec("ARBORA-IDENTITY-R004.7.3")
def test_none_mode_refuses_a_public_listener_and_an_empty_declaration():
    with pytest.raises(IdentityUnavailable, match="loopback"):
        identity_factory_kwargs(
            "none",
            None,
            factory=_factory_with_policy,
            listener_host="0.0.0.0",
            read_routes=lambda: _DECLARED,
        )
    with pytest.raises(IdentityUnavailable, match="declares no anonymous read"):
        identity_factory_kwargs(
            "none",
            None,
            factory=_factory_with_policy,
            listener_host="127.0.0.1",
            read_routes=lambda: frozenset(),
        )

    def no_policy_factory(agent, *, auth_mode=None):
        pass

    with pytest.raises(IdentityUnavailable, match="cannot take"):
        identity_factory_kwargs(
            "none",
            None,
            factory=no_policy_factory,
            listener_host="127.0.0.1",
            read_routes=lambda: _DECLARED,
        )


@pytest.mark.parametrize("mode", ["required", "oidc"])
@pytest.mark.spec("ARBORA-IDENTITY-R004.7.3")
def test_no_read_policy_in_any_other_mode(mode):
    values = identity_factory_kwargs(
        mode,
        None,
        factory=_factory_with_policy,
        listener_host="127.0.0.1",
        read_routes=lambda: _DECLARED,
    )
    assert "anonymous_read_routes" not in values
