"""ARBORA-HOST-R039.3: the host's policy state is owned, never module-global."""

from __future__ import annotations

import pytest

import arbora.access.action_policy as ap
from arbora.access.policy_state import PolicyState
from arbora.mcp_server.host_context import build_host_context

pytestmark = pytest.mark.spec("ARBORA-HOST-R039.3")


def test_two_policy_states_do_not_share_a_cache() -> None:
    engine = object()
    first, second = PolicyState(), PolicyState()

    gate = first.action_policies.for_engine(engine)

    assert first.action_policies.for_engine(engine) is gate
    assert second.action_policies.for_engine(engine) is not gate
    assert first.action_policies.for_engine() is not second.action_policies.for_engine()


def test_invalidate_drops_only_its_own_gates() -> None:
    engine = object()
    first, second = PolicyState(), PolicyState()
    kept = second.action_policies.for_engine(engine)
    dropped = first.action_policies.for_engine(engine)

    first.action_policies.invalidate()

    assert first.action_policies.for_engine(engine) is not dropped
    assert second.action_policies.for_engine(engine) is kept


def test_every_gate_reads_the_ports_of_its_own_state() -> None:
    seen: list[str] = []
    bound = ap.ActionPolicies(ap.ActionPolicyPorts(notifier=seen.append))

    bound.for_engine(object())._notify("to the bound notifier")
    ap.ActionPolicies().for_engine(object())._notify("logged only")

    assert seen == ["to the bound notifier"]


def test_the_host_context_owns_one_policy_state() -> None:
    first = build_host_context(lambda: None)
    second = build_host_context(lambda: None)

    assert isinstance(first.policy, PolicyState)
    assert first.policy.action_policies is not second.policy.action_policies


def test_no_module_policy_global_remains() -> None:
    for name in (
        "_ports",
        "_POLICY_CACHE",
        "_NO_ENGINE_POLICY",
        "_policy_cache_lock",
        "get_action_policy",
        "bind_action_policy_ports",
        "reset_action_policy_cache_for_tests",
    ):
        assert not hasattr(ap, name), name
