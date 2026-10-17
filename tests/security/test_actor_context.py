"""The ambient actor context binds, nests and fails closed."""

from __future__ import annotations

import pytest

from arbora.security.actor_context import (
    ActorContext,
    ActorType,
    IdentityRequiredError,
    current_actor,
    use_actor,
)


def test_no_bound_actor_fails_closed() -> None:
    with pytest.raises(IdentityRequiredError):
        current_actor()


def test_use_actor_nests_and_restores() -> None:
    outer = ActorContext(actor_id="a", actor_type=ActorType.HUMAN, authenticated=True)
    inner = ActorContext(actor_id="b", authenticated=True)
    with use_actor(outer):
        with use_actor(inner):
            assert current_actor() is inner
        assert current_actor() is outer
    with pytest.raises(IdentityRequiredError):
        current_actor()
